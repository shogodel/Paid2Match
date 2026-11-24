"""Multi-currency support (foundation) tests.

CAD/EUR/USD on bounties, default CAD, no FX conversion anywhere:
amounts are recorded and shown as entered, in the need's currency.
"""
import pytest
from decimal import Decimal
from flask import render_template_string

from extensions import db
from models.bounty import Bounty
from utils.currency import (
    CURRENCIES,
    CURRENCY_SYMBOLS,
    DEFAULT_CURRENCY,
    format_money,
    validate_currency,
)


# ------------------------------------------------------------------ utils


class TestCurrencyConstants:
    def test_currencies(self):
        assert CURRENCIES == ['CAD', 'EUR', 'USD']

    def test_default_is_cad(self):
        assert DEFAULT_CURRENCY == 'CAD'

    def test_symbols_unambiguous(self):
        assert CURRENCY_SYMBOLS == {
            'CAD': 'CA$', 'EUR': '€', 'USD': 'US$',
        }


class TestValidateCurrency:
    @pytest.mark.parametrize('code', ['CAD', 'EUR', 'USD'])
    def test_accepts_all_three(self, code):
        assert validate_currency(code) == code

    @pytest.mark.parametrize('code', ['cad', 'eur', 'usd', 'Cad'])
    def test_case_insensitive(self, code):
        assert validate_currency(code) == code.upper()

    @pytest.mark.parametrize('code', ['GBP', 'JPY', '', 'CADD', None, 123])
    def test_rejects_anything_else(self, code):
        with pytest.raises(ValueError):
            validate_currency(code)


class TestFormatMoney:
    def test_cad_symbol_and_format(self):
        assert format_money(1000, 'CAD') == 'CA$1,000.00'

    def test_eur_symbol(self):
        assert format_money(2500.5, 'EUR') == '€2,500.50'

    def test_usd_symbol(self):
        assert format_money(99, 'USD') == 'US$99.00'

    def test_lowercase_code(self):
        # Existing Payout rows store lowercase 'cad'.
        assert format_money(1000, 'cad') == 'CA$1,000.00'
        assert format_money(1000, 'eur') == '€1,000.00'

    def test_default_currency_is_cad(self):
        assert format_money(1000) == 'CA$1,000.00'
        assert format_money(1000, None) == 'CA$1,000.00'

    def test_decimal_and_string_amounts(self):
        assert format_money(Decimal('1234.5'), 'USD') == 'US$1,234.50'
        assert format_money('42', 'EUR') == '€42.00'

    def test_none_amount_renders_empty(self):
        assert format_money(None, 'CAD') == ''

    def test_invalid_currency_raises(self):
        with pytest.raises(ValueError):
            format_money(100, 'GBP')


class TestMoneyFilter:
    # render_template_string runs the app's context processors, which
    # read current_user — so these need a request context, not just
    # an app context.
    def test_filter_registered_and_renders(self, app):
        with app.test_request_context():
            assert render_template_string(
                '{{ 1000|money("eur") }}') == '€1,000.00'
            assert render_template_string(
                '{{ 2500.5|money("USD") }}') == 'US$2,500.50'

    def test_filter_defaults_to_cad(self, app):
        with app.test_request_context():
            assert render_template_string(
                '{{ 100|money }}') == 'CA$100.00'

    def test_filter_with_bounty_currency(self, app, sample_paid_bounty):
        with app.app_context():
            bounty = db.session.get(Bounty, sample_paid_bounty.id)
            bounty.currency = 'EUR'
            db.session.commit()
            # Nested request context: the render needs it (context
            # processors read current_user), the bounty needs the
            # session the app context holds.
            with app.test_request_context():
                rendered = render_template_string(
                    '{{ bounty.reward_amount|money(bounty.currency) }}',
                    bounty=bounty,
                )
            assert rendered == '€5,000.00'


# ------------------------------------------------------------------ model


def _bounty_kwargs(poster_id, **overrides):
    kwargs = {
        'poster_id': poster_id,
        'title': 'Currency test bounty for validation purposes',
        'description': 'A bounty created to test the currency column '
                       'and its validation behaviour end to end.',
        'bounty_type': 'recruitment',
        'bounty_direction': 'seeking_opportunity',
        'reward_amount': 500,
        'status': 'open',
        'payment_status': 'unsecured',
    }
    kwargs.update(overrides)
    return kwargs


class TestBountyCurrency:
    @pytest.mark.parametrize('code', ['CAD', 'EUR', 'USD'])
    def test_all_three_codes_accepted(self, app, sample_user, code):
        with app.app_context():
            bounty = Bounty(**_bounty_kwargs(sample_user.id, currency=code))
            db.session.add(bounty)
            db.session.commit()
            assert bounty.currency == code

    def test_lowercase_code_normalised(self, app, sample_user):
        with app.app_context():
            bounty = Bounty(**_bounty_kwargs(sample_user.id, currency='eur'))
            db.session.add(bounty)
            db.session.commit()
            assert bounty.currency == 'EUR'

    def test_invalid_code_rejected(self, app, sample_user):
        with app.app_context():
            with pytest.raises(ValueError):
                Bounty(**_bounty_kwargs(sample_user.id, currency='GBP'))

    def test_default_is_cad(self, app, sample_user):
        with app.app_context():
            bounty = Bounty(**_bounty_kwargs(sample_user.id))
            db.session.add(bounty)
            db.session.commit()
            db.session.refresh(bounty)
            assert bounty.currency == 'CAD'

    def test_display_name_uses_currency(self, app, sample_paid_bounty):
        with app.app_context():
            bounty = db.session.get(Bounty, sample_paid_bounty.id)
            bounty.currency = 'USD'
            assert 'US$5,000.00' in bounty.display_name

    def test_to_dict_includes_currency(self, app, sample_paid_bounty):
        with app.app_context():
            bounty = db.session.get(Bounty, sample_paid_bounty.id)
            assert bounty.to_dict()['currency'] == 'CAD'


# ------------------------------------------------------------------ forms


def _post_data(**overrides):
    data = {
        'title': 'A Currency Bounty For Testing Purposes Here',
        'description': 'This bounty tests that the currency select is '
                       'stored on the need when the form is submitted.',
        'bounty_type': 'recruitment',
        'bounty_direction': 'seeking_opportunity',
        'reward_amount': '2500',
    }
    data.update(overrides)
    return data


class TestBountyCurrencyForm:
    def test_post_form_shows_currency_select(
            self, logged_in_user, sample_user):
        r = logged_in_user.get('/needs/post')
        assert r.status_code == 200
        assert b'name="currency"' in r.data
        assert b'CAD' in r.data
        assert b'EUR' in r.data
        assert b'USD' in r.data
        assert b'Currency' in r.data

    def test_edit_form_shows_currency_select(
            self, logged_in_user, sample_bounty):
        # sample_bounty is posted by sample_user, who is logged in.
        r = logged_in_user.get(f'/needs/{sample_bounty.id}/edit')
        assert r.status_code == 200
        assert b'name="currency"' in r.data

    def test_post_stores_currency(self, logged_in_user, app, sample_user):
        r = logged_in_user.post(
            '/needs/post', data=_post_data(currency='EUR'),
            follow_redirects=False)
        assert r.status_code == 302
        with app.app_context():
            bounty = Bounty.query.order_by(
                Bounty.created_at.desc()).first()
            assert bounty.currency == 'EUR'
            assert bounty.reward_amount == 2500

    def test_post_without_currency_defaults_to_cad(
            self, logged_in_user, app, sample_user):
        r = logged_in_user.post(
            '/needs/post', data=_post_data(),
            follow_redirects=False)
        assert r.status_code == 302
        with app.app_context():
            bounty = Bounty.query.order_by(
                Bounty.created_at.desc()).first()
            assert bounty.currency == 'CAD'

    def test_edit_stores_currency(
            self, logged_in_user, app, sample_bounty):
        r = logged_in_user.post(
            f'/needs/{sample_bounty.id}/edit',
            data=_post_data(currency='USD', reward_amount='5000'),
            follow_redirects=True)
        assert r.status_code == 200
        with app.app_context():
            bounty = db.session.get(Bounty, sample_bounty.id)
            assert bounty.currency == 'USD'

    def test_post_rejects_invalid_currency(
            self, logged_in_user, app, sample_user):
        r = logged_in_user.post(
            '/needs/post', data=_post_data(currency='GBP'),
            follow_redirects=False)
        assert r.status_code == 200  # re-rendered with a form error
        with app.app_context():
            assert Bounty.query.filter_by(
                title=_post_data()['title']).count() == 0


# ------------------------------------------------------------------ display


class TestCurrencyDisplay:
    # The paid fixture is unsecured, so it is not publicly visible —
    # detail tests log in as the poster, who may always view it.
    def test_detail_shows_euro_symbol(
            self, logged_in_user, app, sample_paid_bounty):
        with app.app_context():
            bounty = db.session.get(Bounty, sample_paid_bounty.id)
            bounty.currency = 'EUR'
            db.session.commit()
        r = logged_in_user.get(f'/needs/{sample_paid_bounty.url_slug}')
        assert r.status_code == 200
        assert '€5,000.00' in r.data.decode()

    def test_board_shows_usd_symbol(
            self, client, app, sample_paid_bounty):
        with app.app_context():
            bounty = db.session.get(Bounty, sample_paid_bounty.id)
            bounty.currency = 'USD'
            bounty.status = 'open'
            bounty.payment_status = 'secured'
            db.session.commit()
        r = client.get('/needs/')
        assert r.status_code == 200
        assert 'US$5,000.00' in r.data.decode()

    def test_detail_defaults_to_cad_symbol(
            self, logged_in_user, sample_paid_bounty):
        r = logged_in_user.get(f'/needs/{sample_paid_bounty.url_slug}')
        assert r.status_code == 200
        assert 'CA$5,000.00' in r.data.decode()
