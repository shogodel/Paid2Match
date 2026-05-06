# Stripe Webhook Setup for paid2match.work

## Current Status
- ✅ Webhook endpoint implemented at: `https://paid2match.work/bounties/stripe-webhook`
- ✅ Webhook secret configured in database (AdminSettings)
- ✅ Signature verification enabled
- ✅ Best practices implemented (idempotency, logging, error handling)

## Setup Instructions

### Option 1: Using Stripe CLI (Recommended for testing)

```bash
# Install Stripe CLI
# macOS: brew install stripe/stripe-cli/stripe
# Linux: curl -s https://packages.stripe.dev/api/apt/public.key | sudo apt-key add -
#        echo "deb https://packages.stripe.dev/apt stable main" | sudo tee /etc/apt/sources.list.d/stripe.list
#        sudo apt-get update && sudo apt-get install stripe

# Login to Stripe
stripe login

# Forward webhooks to local development
stripe listen --forward-to localhost:5000/bounties/stripe-webhook

# Copy the webhook signing secret (whsec_...) and update in Stripe Dashboard
```

### Option 2: Configure in Stripe Dashboard (Production)

1. **Login to Stripe Dashboard**: https://dashboard.stripe.com/
2. **Navigate to Webhooks**: Developers → Webhooks
3. **Add Endpoint**:
   - Endpoint URL: `https://paid2match.work/bounties/stripe-webhook`
   - Description: `Paid2Match Production Webhook`
   - Events to send:
     - ✅ `checkout.session.completed` (handles bounty payments & upgrades)
     - ✅ `payment_intent.succeeded` (optional, for tracking)
     - ✅ `payment_intent.payment_failed` (optional, for failure handling)

4. **Copy the Signing Secret**:
   - After creating the webhook, click on it
   - Click "Reveal" next to "Signing secret"
   - Copy the `whsec_...` value

5. **Update in Paid2Match Admin Settings**:
   - Login to admin panel: `https://paid2match.work/admin`
   - Navigate to Settings
   - Update `STRIPE_WEBHOOK_SECRET` with the new value
   - Or update directly in database: AdminSettings.set('STRIPE_WEBHOOK_SECRET', 'whsec_...')

## Testing the Webhook

### Test with curl (requires valid signature):
```bash
# Get the webhook secret from admin settings
WEBHOOK_SECRET="whsec_..."

# Create a test payload
PAYLOAD='{"id":"evt_test123","type":"checkout.session.completed","data":{"object":{"id":"cs_test123","metadata":{"bounty_id":"test123","upgrade_session":"false"},"payment_intent":"pi_test123"}}}'

# Generate signature (requires stripe library)
# Or use Stripe CLI to send test webhook:
stripe trigger checkout.session.completed
```

### Monitor Logs:
```bash
# Check application logs for webhook messages
tail -f /var/log/paid2match/app.log | grep -i webhook

# Or check in real-time via Flask logging
```

## Webhook Event Handling

The webhook currently handles:

1. **checkout.session.completed**:
   - Processes bounty payments (sets payment_status to 'secured')
   - Processes upgrade purchases (creates/extends BountyUpgrade records)
   - Uses idempotency checking to prevent duplicate processing

2. **payment_intent.succeeded** (logged, not fully handled)

3. **payment_intent.payment_failed** (logged, not fully handled)

## Security Features

- ✅ Signature verification (prevents forged webhooks)
- ✅ Idempotency checking (prevents duplicate processing via session ID)
- ✅ Structured logging (for debugging)
- ✅ Quick 200 response (acknowledges receipt promptly)
- ✅ Error handling (graceful failure handling)

## Troubleshooting

### Webhook returning 400:
- Check that `STRIPE_WEBHOOK_SECRET` is correctly set in AdminSettings
- Verify the webhook signature in the request header
- Check application logs for "Invalid webhook signature" errors

### Events not processing:
- Verify the webhook is configured in Stripe Dashboard
- Check that the correct events are selected (checkout.session.completed)
- Monitor Flask application logs

### HTTPS Required:
- Stripe requires HTTPS for webhook endpoints in production
- Ensure SSL certificate is properly configured for paid2match.work
- Check with: `curl -I https://paid2match.work/bounties/stripe-webhook`

## Required Environment Variables

```
STRIPE_API_KEY=sk_live_... (or sk_test_... for testing)
STRIPE_PUBLISHABLE_KEY=pk_live_... (or pk_test_...)
STRIPE_WEBHOOK_SECRET=whsec_... (from Stripe Dashboard webhook configuration)
```

## Next Steps

1. Ensure HTTPS is working for paid2match.work
2. Configure webhook in Stripe Dashboard with production URL
3. Update STRIPE_WEBHOOK_SECRET in admin settings
4. Test with a real checkout session
5. Monitor logs to verify webhook is receiving and processing events
