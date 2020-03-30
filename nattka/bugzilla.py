""" Bugzilla support. """

import enum
import json
import typing

import requests


BUGZILLA_API_URL = 'https://bugs.gentoo.org/rest'

INCLUDE_BUG_FIELDS = (
    'id',
    'product',
    'component',
    'cf_stabilisation_atoms',
    'cc',
    'depends_on',
    'blocks',
    'flags',
)


class BugCategory(enum.Enum):
    KEYWORDREQ = enum.auto()
    STABLEREQ = enum.auto()

    @classmethod
    def from_product_component(cls, product: str, component: str
            ) -> typing.Optional['BugCategory']:
        """
        Return a BugCategory for bug in @product and @component.
        """

        if product == 'Gentoo Linux':
            if component == 'Keywording':
                return cls.KEYWORDREQ
            elif component == 'Stabilization':
                return cls.STABLEREQ
        elif product == 'Gentoo Security':
            if component in ('Vulnerabilities', 'Kernel'):
                return cls.STABLEREQ
        return None

    @classmethod
    def to_products_components(cls, val: 'BugCategory'
            ) -> typing.Tuple[typing.List[str], typing.List[str]]:
        """
        Return a tuple of valid bug products and components for a given
        category.
        """

        if val == cls.KEYWORDREQ:
            return (['Gentoo Linux'], ['Keywording'])
        elif val == cls.STABLEREQ:
            return (['Gentoo Linux', 'Gentoo Security'],
                    ['Stabilization', 'Vulnerabilities', 'Kernel'])
        else:
            return (['Gentoo Linux', 'Gentoo Security'],
                    ['Keywording', 'Stabilization', 'Vulnerabilities',
                     'Kernel'])


class BugInfo(typing.NamedTuple):
    category: BugCategory
    atoms: str
    cc: typing.List[str]
    depends: typing.List[int]
    blocks: typing.List[int]
    sanity_check: typing.Optional[bool]


def make_bug_info(bug: typing.Dict[str, typing.Any]) -> BugInfo:
    bcat = BugCategory.from_product_component(bug['product'],
                                              bug['component'])
    atoms = bug['cf_stabilisation_atoms'] + '\r\n'
    sanity_check = None
    for f in bug['flags']:
        if f['name'] == 'sanity-check':
            if f['status'] == '+':
                sanity_check = True
            elif f['status'] == '-':
                sanity_check = False

    return BugInfo(bcat, atoms, bug['cc'], bug['depends_on'],
                   bug['blocks'], sanity_check)


class NattkaBugzilla(object):
    def __init__(self, api_key: str, api_url: str = BUGZILLA_API_URL,
                 auth: typing.Tuple[str, str] = None):
        self.api_key = api_key
        self.api_url = api_url
        self.auth = auth
        self.session = requests.Session()

    def _request(self, endpoint: str, params: typing.Mapping[str,
            typing.Union[typing.Iterable[str], str]]) -> requests.Response:
        params = dict(params)
        params['Bugzilla_api_key'] = self.api_key
        params['include_fields'] = INCLUDE_BUG_FIELDS
        ret = self.session.get(self.api_url + '/' + endpoint,
                               auth=self.auth,
                               params=params)
        ret.raise_for_status()
        return ret

    def _request_put(self, endpoint: str, data: dict) -> requests.Response:
        data = dict(data)
        ret = self.session.put(self.api_url + '/' + endpoint,
                               auth=self.auth,
                               json=data,
                               params={
                                   'Bugzilla_api_key': self.api_key,
                               })
        ret.raise_for_status()
        return ret

    def fetch_package_list(self, bugs: typing.Iterable[int]
            ) -> typing.Dict[int, BugInfo]:
        """
        Fetch specified @bugs (list of bug numbers).  Returns a dict
        of {bugno: buginfo}.
        """

        resp = self._request('bug',
                             params={
                                 'id': ','.join(str(x) for x in bugs)
                             }).json()

        ret = {}
        for b in resp['bugs']:
            ret[b['id']] = make_bug_info(b)
        return ret

    def find_bugs(self, category: BugCategory, limit: int = None
            ) -> typing.Dict[int, BugInfo]:
        """
        Find all relevant bugs in @category.  Limit to @limit results
        (None = no limit).
        """

        product, component = BugCategory.to_products_components(category)
        search_params = {
            'product': product,
            'component': component,
            'resolution': '---',
        }
        if limit is not None:
            search_params['limit'] = str(limit)

        resp = self._request('bug', params=search_params).json()

        ret = {}
        for b in resp['bugs']:
            # skip empty bugs (likely security issues that are not
            # stabilization requests)
            if not b['cf_stabilisation_atoms'].strip():
                continue
            ret[b['id']] = make_bug_info(b)
        return ret

    def update_status(self, bugno: int, status: bool, comment: str = None):
        """
        Update the sanity-check status of bug @bugno.  @status specifies
        the new status, @comment is an optional comment to add.
        """

        req = {
            'ids': [bugno],
            'flags': [
                {
                    'name': 'sanity-check',
                    'status': '+' if status else '-',
                },
            ],
        }

        if comment is not None:
            req['comment'] = {
                'body': comment,
            }

        resp = self._request_put(f'bug/{bugno}', data=req).json()
        assert resp['bugs'][0]['id'] == bugno


def get_combined_buginfo(bugdict: typing.Dict[int, BugInfo], bugno: int
        ) -> BugInfo:
    """
    Combine information from linked (via 'depends on') bugs into
    a single BugInfo.  @bugdict is the dict returned by Bugzilla search,
    @bugno is the number of bug of interest.
    """

    topbug = bugdict[bugno]
    combined_bugs = [topbug]
    atoms = ''
    deps = set()

    i = 0
    while i < len(combined_bugs):
        atoms += combined_bugs[i].atoms
        for b in combined_bugs[i].depends:
            if b in bugdict and bugdict[b].category == topbug.category:
                combined_bugs.append(bugdict[b])
            else:
                deps.add(b)
        i += 1

    return BugInfo(topbug.category, atoms, topbug.cc, sorted(deps),
                   topbug.blocks, topbug.sanity_check)


def fill_keywords_from_cc(bug: BugInfo, known_arches: typing.Iterable[str]
        ) -> BugInfo:
    """
    Fill missing keywords in @bug based on CC list.  @known_arches
    specifies the list of valid arches.  Returns a BugInfo with arches
    filled in (if possible).
    """

    arches = sorted(x.split('@')[0] for x in frozenset(f'{x}@gentoo.org'
                                                       for x
                                                       in known_arches)
                                             .intersection(bug.cc))

    ret = ''
    for l in bug.atoms.splitlines():
        sl = l.split()
        if len(sl) == 1:
            l += f' {" ".join(arches)}'
        ret += f'{l}\r\n'

    return BugInfo(bug.category, ret, bug.cc, bug.depends, bug.blocks,
                   bug.sanity_check)
