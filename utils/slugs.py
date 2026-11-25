"""Nice URLs (slugs, shortcodes, handles).

Needs live at /needs/<slug>-<shortcode> and profiles at
/profile/<handle>. The pure string functions here know nothing about
the database; the ``unique_*`` helpers take a ``taken`` predicate so
the models (before_insert events) and the Alembic backfill can both
use them with whatever lookup they have.
"""
import re
import secrets
import unicodedata

#: Shortcode alphabet — lowercase letters + digits, no lookalikes
#: removed on purpose (the code is never typed by hand, it is only
#: ever clicked or copied).
SHORTCODE_ALPHABET = 'abcdefghijklmnopqrstuvwxyz0123456789'
SHORTCODE_LENGTH = 6
#: Slugs are trimmed to about this many characters so the URL stays
#: readable; the shortcode after it is what actually identifies the
#: need.
SLUG_MAX_LENGTH = 80

#: Handles: 3–30 lowercase letters, digits, dots, dashes and
#: underscores. Enforced in the model (@validates) and the form.
HANDLE_RE = re.compile(r'^[a-z0-9._-]{3,30}$')
HANDLE_MIN_LENGTH = 3
HANDLE_MAX_LENGTH = 30

#: Handles nobody may take. The /profile/* static routes first: a
#: handle equal to one of those segments would be unreachable —
#: Werkzeug matches the static segment before the <handle> rule,
#: so /profile/setup would show the setup form, never the profile.
#: The rest are top-level route prefixes and app words a handle
#: would impersonate or read as a site section (/profile/admin
#: looks official; 'needs', 'wins', 'pledge' read as site pages).
RESERVED_HANDLES = frozenset({
    # /profile/* static routes.
    'setup', 'edit', 'linked', 'verify-request', 'users',
    # Top-level route prefixes.
    'admin', 'api', 'static', 'auth', 'needs', 'bounties', 'wins',
    'pledge', 'pledges', 'intros', 'stakes', 'messages', 'referrals',
    'social', 'video-vouches',
    # Account / app words.
    'login', 'logout', 'signup', 'register', 'settings', 'search',
    'support', 'help', 'about', 'root', 'system', 'official',
    'paid2match', 'me', 'new',
})


def _transliterate(value):
    """Strip accents: é→e, ñ→n, ü→u (NFKD + drop non-ASCII)."""
    normalized = unicodedata.normalize('NFKD', value or '')
    return normalized.encode('ascii', 'ignore').decode('ascii')


def slugify(value):
    """'Électricien à Montréal' → 'electricien-a-montreal'.

    Lowercases, keeps [a-z0-9] joined by single '-', collapses
    repeats, trims to ~80 chars. An empty result (blank title, or
    a title with no Latin characters at all) becomes 'need'.
    """
    ascii_value = _transliterate(value).lower()
    slug = re.sub(r'[^a-z0-9]+', '-', ascii_value).strip('-')
    if len(slug) > SLUG_MAX_LENGTH:
        # Cut at the last '-' inside the limit so we never split a
        # word; if there is no '-' at all, hard-cut at the limit.
        cut = slug[:SLUG_MAX_LENGTH]
        hyphen = cut.rfind('-')
        slug = cut[:hyphen] if hyphen > 0 else cut
        slug = slug.rstrip('-')
    return slug or 'need'


def generate_shortcode():
    """A random 6-character code from [a-z0-9]."""
    return ''.join(
        secrets.choice(SHORTCODE_ALPHABET) for _ in range(SHORTCODE_LENGTH)
    )


def unique_shortcode(taken, tries=10):
    """A shortcode ``taken(code)`` reports as unused.

    Retries a few times on collision (the space is 36**6 ≈ 2.2B, so
    a collision is a fluke, not a flow); raises RuntimeError if the
    space were ever exhausted.
    """
    for _ in range(tries):
        code = generate_shortcode()
        if not taken(code):
            return code
    raise RuntimeError('could not generate a unique shortcode')


def parse_slug_code(slug_code):
    """Split 'electricien-a-montreal-x1y2z3' into
    ('electricien-a-montreal', 'x1y2z3'). Returns (None, None) when
    the value has no trailing -XXXXXX shortcode."""
    head, sep, tail = (slug_code or '').rpartition('-')
    if not sep or not head:
        return None, None
    if len(tail) != SHORTCODE_LENGTH or not re.fullmatch(
        r'[a-z0-9]{6}', tail
    ):
        return None, None
    return head, tail


def handle_from_name(full_name, taken=None):
    """Suggest a handle from a display name: 'Test User' →
    'test.user'. Transliterates, lowercases, turns spaces into '.',
    strips anything outside [a-z0-9._-], pads/truncates to 3–30
    characters. When ``taken(handle)`` is given, appends digits
    ('test.user2', 'test.user3', …) until the handle is free.
    Reserved handles (RESERVED_HANDLES) are never returned — they
    count as taken, so 'Admin' becomes 'admin2'."""
    base = _transliterate(full_name).lower()
    base = re.sub(r'\s+', '.', base)
    base = re.sub(r'[^a-z0-9._-]', '', base)
    base = re.sub(r'[._-]+', lambda m: m.group(0)[0], base)
    base = base.strip('._-')
    if not base:
        base = 'user'
    if len(base) < HANDLE_MIN_LENGTH:
        base = base + 'x' * (HANDLE_MIN_LENGTH - len(base))
    if len(base) > HANDLE_MAX_LENGTH:
        base = base[:HANDLE_MAX_LENGTH].rstrip('._-')

    def _unavailable(handle):
        # Reserved handles are never handed out — treat them as
        # taken so generation appends digits ('Admin' -> 'admin2',
        # never 'admin').
        return handle in RESERVED_HANDLES or (taken and taken(handle))

    if not _unavailable(base):
        return base
    # Collision: append digits, shrinking the base to fit 30 chars.
    n = 2
    while True:
        suffix = str(n)
        candidate = (
            base[:HANDLE_MAX_LENGTH - len(suffix)].rstrip('._-') + suffix
        )
        if not _unavailable(candidate):
            return candidate
        n += 1


def is_valid_handle(value):
    """True when value matches ^[a-z0-9._-]{3,30}$."""
    return bool(value) and bool(HANDLE_RE.match(value))


def is_reserved_handle(value):
    """True when the handle is reserved (see RESERVED_HANDLES) —
    a route segment or app word nobody may take as a handle."""
    return bool(value) and value in RESERVED_HANDLES
