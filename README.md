# Paid2Match

A recruitment-only talent marketplace connecting employers with recruiters and candidates. Employers post opportunities, recruiters source and verify real people, and payment is released on verified placement.

## What it does

- 💼 **Recruitment** - Employers post opportunities, recruiters source and vet candidates, and payment is released on verified placement.

## Features

- Free bounty posting with optional payment securing
- Three-way bounty mechanics (seeking/offering/opportunity)
- Pitch-based system (anonymous until interest shown)
- Dual confirmation matching
- Multi-language support (EN/FR/ES)
- Premium bounty upgrades (highlight, urgent, featured)
- Affiliate program with multi-level commissions
- Admin panel with moderation tools

## Tech Stack

- **Backend:** Flask (Python)
- **Database:** PostgreSQL
- **Auth:** Flask-Login
- **Payments:** Stripe Connect
- **Server:** uWSGI + Nginx
- **Hosting:** DigitalOcean

## Setup

### Requirements

- Python 3.12+
- PostgreSQL 14+
- Nginx
- uWSGI

### Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/yourusername/paid2match.git
   cd paid2match
   ```

2. Create virtual environment:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

4. Create .env file:
   ```bash
   cp .env.example .env
   # Edit .env with your actual values
   ```

5. Initialize database:
   ```bash
   flask db upgrade
   ```

6. Create admin user:
   ```bash
   flask shell
   >>> from models.user import User
   >>> from werkzeug.security import generate_password_hash
   >>> admin = User(email='admin@paid2match.work', 
                    password_hash=generate_password_hash('your-password'),
                    full_name='Admin', role='admin')
   >>> db.session.add(admin)
   >>> db.session.commit()
   ```

7. Run development server:
   ```bash
   flask run
   ```

## Production Deployment

See DEPLOYMENT.md for production setup instructions.

## Translations (i18n)

The UI is translated into French and Spanish (English is the source language).
Strings are marked with `_()` in templates and `lazy_gettext` (aliased `_l`)
in Python; `babel.cfg` lists the keywords pybabel extracts (including `_l`).

Workflow after changing any user-facing string:

```bash
pybabel extract -F babel.cfg -o messages.pot .
pybabel update -i messages.pot -d translations
# edit translations/{fr,es}/LC_MESSAGES/messages.po
pybabel compile -d translations
```

Commit the `.po` **and** the compiled `.mo` files. The Dockerfile also runs
`pybabel compile` at image build, so a fresh build never ships stale
translations. The language switcher (`/set-language/<lang>`) returns the user
to the page they were on and persists the choice on the profile when logged in.

## License

Private - All Rights Reserved
