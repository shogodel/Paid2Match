# Email setup for Paid2Match

Signup email confirmation and password reset send real email via Flask-Mail
(SMTP). All settings come from environment variables — there are no
hardcoded credentials anywhere in the code.

## Variables to set on the server (.env)

| Variable | Required | What it is |
|---|---|---|
| `MAIL_SERVER` | Yes | SMTP host, e.g. `smtp.sendgrid.net` or `smtp.gmail.com` |
| `MAIL_PORT` | No (default 587) | SMTP port: 587 for STARTTLS, 465 for SSL |
| `MAIL_USE_TLS` | No (default `true`) | STARTTLS — use with port 587 |
| `MAIL_USE_SSL` | No (default `false`) | Implicit SSL — use with port 465 instead of TLS |
| `MAIL_USERNAME` | Yes | SMTP login |
| `MAIL_PASSWORD` | Yes | SMTP password / API key (see options below) |
| `MAIL_DEFAULT_SENDER` | Yes | From address shown to users, e.g. `noreply@paid2match.work` |

If `MAIL_SERVER` is left unset, the app still runs, but no email is sent:
new signups are created unverified and cannot log in until an admin marks
them verified from the admin user list ("Verify Email" button). So mail is
effectively required before real users sign up.

## Option A — SendGrid SMTP relay (recommended for a production site)

1. Create a free SendGrid account and verify a sender identity (a single
   sender email, or domain authentication for `paid2match.work`).
2. Create an API key with "Mail Send" permission.
3. Set:
   ```
   MAIL_SERVER=smtp.sendgrid.net
   MAIL_PORT=587
   MAIL_USE_TLS=true
   MAIL_USE_SSL=false
   MAIL_USERNAME=apikey
   MAIL_PASSWORD=<the SendGrid API key>
   MAIL_DEFAULT_SENDER=noreply@paid2match.work
   ```
   (The username is literally the word `apikey`; the password is the key.)

## Option B — Gmail app password (simplest while the user base is tiny)

1. Use a Gmail account with 2-Step Verification turned on.
2. Create an app password: Google Account → Security → 2-Step Verification
   → App passwords → generate one for "Mail".
3. Set:
   ```
   MAIL_SERVER=smtp.gmail.com
   MAIL_PORT=587
   MAIL_USE_TLS=true
   MAIL_USE_SSL=false
   MAIL_USERNAME=<the Gmail address>
   MAIL_PASSWORD=<the 16-character app password, no spaces>
   MAIL_DEFAULT_SENDER=<the Gmail address>
   ```
   Note: Gmail's daily sending limits (~500/day for personal accounts) are
   fine for confirmation/reset mail at this scale, but this is not a
   long-term bulk-mail solution.

## What gets sent

- **Signup confirmation** — subject "Confirm your Paid2Match account",
  link valid 24 hours (`EMAIL_CONFIRM_TOKEN_MAX_AGE` in config.py).
- **Password reset** — subject "Reset your Paid2Match password",
  link valid 1 hour (`PASSWORD_RESET_TOKEN_MAX_AGE`).

Both links carry a signed, expiring token (itsdangerous, keyed by
`SECRET_KEY`). Tokens are single-purpose (different salts) and cannot be
forged without the secret key.

## Notes

- The reset and resend-verification forms always show the same message
  whether or not the email exists, so they cannot be used to probe for
  accounts.
- Existing accounts (created before this change) were grandfathered as
  verified by the `m0_foundation` migration — they are not affected.
- After changing `.env`, restart uWSGI for the new values to take effect.
