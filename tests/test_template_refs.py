"""Template reference integrity.

Every url_for endpoint and static file a template references must
exist: a bad endpoint raises BuildError at render time (a 500 for
the visitor), a missing static file is a silent 404. The Nov 2026
frontend audit verified all 154 literal url_for endpoints and all
static refs across the templates; these tests lock that in.

Also locks in that no template links the deleted /auth/register
route (removed in the OAuth-only build).
"""
import os
import re

TEMPLATES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'templates')

# url_for('endpoint', ...) with a literal endpoint name. Calls with a
# variable endpoint are skipped (nothing to check statically).
URLFOR_RE = re.compile(r"""url_for\(\s*['"]([^'"]+)['"]""")
# url_for('static', filename='...') with a literal filename.
STATIC_RE = re.compile(
    r"""url_for\(\s*['"]static['"]\s*,\s*filename\s*=\s*['"]([^'"]+)['"]""")


def _template_files():
    for dirpath, _, filenames in os.walk(TEMPLATES_DIR):
        for filename in filenames:
            if filename.endswith('.html'):
                yield os.path.join(dirpath, filename)


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


class TestTemplateUrlFor:
    def test_all_url_for_endpoints_exist(self, app):
        endpoints = {rule.endpoint for rule in app.url_map.iter_rules()}
        missing = []
        for path in _template_files():
            for endpoint in URLFOR_RE.findall(_read(path)):
                if endpoint not in endpoints:
                    missing.append(f'{os.path.basename(path)}: {endpoint}')
        assert not missing, f'url_for endpoints with no route: {missing}'

    def test_all_static_files_exist(self, app):
        static = os.path.join(app.root_path, 'static')
        missing = []
        for path in _template_files():
            for filename in STATIC_RE.findall(_read(path)):
                if not os.path.exists(os.path.join(static, filename)):
                    missing.append(f'{os.path.basename(path)}: {filename}')
        assert not missing, f'static files referenced but missing: {missing}'

    def test_no_template_links_deleted_register_route(self):
        """auth.register was deleted in the OAuth-only build; any
        template still linking it would 500 (url_for) or 404 (href)."""
        hits = []
        for path in _template_files():
            text = _read(path)
            if 'auth.register' in text or '/auth/register' in text:
                hits.append(os.path.basename(path))
        assert not hits, f'templates linking deleted register route: {hits}'
