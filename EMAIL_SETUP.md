# Email setup for Paid2Match

Referral invites, notifications, and the weekly digest send real email
through the Resend HTTPS API when `RESEND_API_KEY` is set, and fall
back to Flask-Mail (SMTP) otherwise. All settings come from
environment variables — there are no hardcoded credentials anywhere
in the code. Auth is OAuth-only (Google, GitHub, Facebook, LinkedIn),
so there is no signup confirmation or password reset mail anymore.

## Variables to set on the server (.env)

Live path first — Resend is what the droplet uses (SMTP ports are
blocked there, see Option A):

| Variable | Required | What it is |
|---|---|---|
| `RESEND_API_KEY` | Yes (live) | Resend API key with "Sending access" |
| `MAIL_DEFAULT_SENDER` | Yes | From address shown to users, e.g. `support@paid2match.work` |
| `MAIL_SERVER` | Only for SMTP fallback | SMTP host, e.g. `smtp.sendgrid.net` or `smtp.gmail.com` |
| `MAIL_PORT` | No (default 587) | SMTP port: 587 for STARTTLS, 465 for SSL |
| `MAIL_USE_TLS` | No (default `true`) | STARTTLS — use with port 587 |
| `MAIL_USE_SSL` | No (default `false`) | Implicit SSL — use with port 465 instead of TLS |
| `MAIL_USERNAME` | Only for SMTP fallback | SMTP login |
| `MAIL_PASSWORD` | Only for SMTP fallback | SMTP password / API key (see options below) |

If neither `RESEND_API_KEY` nor `MAIL_SERVER` is set, the app still
runs, but no email is sent: referral invites, notifications, and the
weekly digest are silently skipped. Sign-in still works — OAuth
providers verify the email.

## Option A — Resend HTTPS API (the live setup, required on this droplet)

DigitalOcean blocks outbound SMTP ports (25/465/587/2525) from the
droplet, so Options B and C cannot send from the live server. When
`RESEND_API_KEY` is set, the app sends through the Resend HTTPS API
(port 443, open) instead of SMTP, and ignores the SMTP settings.
Configured since 2026-11-23 (free tier, 100/day): domain
`paid2match.work` verified, sender `support@paid2match.work`.

1. Create a free Resend account and verify the sending domain
   (`paid2match.work`) with the DNS records Resend shows (DKIM + SPF).
2. Create an API key with "Sending access" permission.
3. Set:
   ```
   RESEND_API_KEY=re_...
   MAIL_DEFAULT_SENDER=support@paid2match.work
   ```
   The SMTP variables are kept as a fallback for hosts where SMTP works.

## Option B — SendGrid SMTP relay (fallback for hosts where SMTP works)

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

## Option C — Gmail app password (simplest while the user base is tiny, SMTP hosts only)

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

- **Referral invites** — subject "Someone referred you on Paid2Match",
  with the consent link for the referred person.
- **Notifications** — account, referral, and payout updates.
- **Weekly digest** — the `flask send-weekly-digest` CLI command.

## Notes

- The admin user list still has a "Verify Email" button for manually
  verifying an account (OAuth sign-ins verify automatically).
- Existing accounts (created before this change) were grandfathered as
  verified by the `m0_foundation` migration — they are not affected.
- After changing `.env`, restart the container for the new values to
  take effect (`docker compose up -d paid2match` in `/home/bogdan/apps`).
