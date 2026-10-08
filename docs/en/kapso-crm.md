# Kapso CRM: setup and pilot

One installation serves one agency. Each client has its own contacts, inbox,
opportunities, appointments, agents, WhatsApp lines and portal users. Agency
staff assign plans and record payments. Portal administrators configure
automations and connect WhatsApp; employees work their business's CRM and inbox.

## Configure the server

Set these variables in the API environment, never in browser build arguments:

```dotenv
KAPSO_API_KEY=<Kapso project API key>
KAPSO_WEBHOOK_SECRET=<random webhook secret>
FRONTEND_URL=https://crm.example.com
FOLLOW_UP_SWEEP_SECONDS=15
COOKIE_SECURE=true
```

Generate the webhook secret with `openssl rand -hex 32` and keep it in the
deployment secret store. After rotating it, use **Verify connection** on each
line to update its webhook. Provide public HTTPS through your reverse proxy;
the existing gateway forwards `/api/*` to the API. Keep `ENCRYPTION_KEY` stable.

Rebuild the API and web services after installing this release. Docker runs
`alembic upgrade head` on API startup; local deployments must run it first.
Migration `0054_crm_kapso` adds the CRM tables and durable webhook queue.
The Kapso worker starts when its key is configured. Setting
`FOLLOW_UP_SWEEP_SECONDS=0` disables follow-ups and appointment reminders.

## Configure a business

1. Create the client, set its timezone to `America/Guatemala`, and create an
   active agent with a working AI provider and model.
2. Open **CRM → Automation** and enter verified hours, address, services and
   prices. Existing agent instructions, knowledge and handoff rules still apply.
3. Enable automatic leads to create an opportunity when a visitor writes.
   The bot can qualify it using the customer's stated needs.
4. Enable appointments; configure weekdays, opening/closing hours, slot duration
   and resources (for example, people, rooms or desks). Concurrent bookings
   cannot overlap for the same business and resource.
5. Create portal users before enabling the portal. Assign `admin` only to people
   who should configure the business; employees use `agent`.

The workspace shows at most 500 recent contacts/opportunities and 500 upcoming
appointments. The existing contact and inbox screens provide larger histories.

## Connect WhatsApp

### Use a number already registered in Kapso

In the agency panel, open the client's **CRM → WhatsApp → Use an existing
Kapso number** (also available when adding an API number under Channels).
The server lists numbers from the project configured by `KAPSO_API_KEY`.
Choose the number, select an answering agent of this client, optionally enter
an inbox name, then click **Link number**. Use the page controls if the project
has more than 100 numbers. Numbers assigned to another local client or a
direct Meta channel are excluded and cannot be imported.

This flow reuses the existing registration and Meta credentials in Kapso.
It does not run embedded signup, create a Kapso customer, move the remote
number, or change its funding. Each imported line pins its exact phone number
ID and Kapso customer (including project numbers without a customer), so a
portal refresh cannot select a sibling number. A change in remote ownership
blocks verification and clears the local routing credential for agency review.
Only agency users can list or import project numbers; client portals can
verify their own lines.

Linking requires the server credentials above and a public HTTPS
`FRONTEND_URL`. It reconciles the existing signed Meta webhook and uses the
same inbox, agent, delivery and reminder pipeline. If webhook registration
fails, the line remains visible with its error: resolve it and use **Verify
connection**. Another application's Meta webhook is never replaced silently.
Review existing Kapso workflows before using the platform's agent to avoid
duplicate automatic replies. A disconnected number must first be restored
in Kapso; project numbers without a customer also reconnect there.

### Connect through hosted setup

Use **CRM → WhatsApp → Open connection page**, select the answering agent and
authorize the number on Kapso's hosted page. Select an existing Kapso line to
reconnect it instead of creating another local line. New numbers in the agency
channel screen also use this flow; existing direct Meta channels remain usable.

The server creates or reconciles a Kapso customer using the client's UUID as
`external_customer_id`, then creates a setup link with
`meta_billing_mode: "partner_managed"`. Customers do not copy Meta tokens or
secrets. Kapso's Meta app handles authorization; messages use Kapso afterward.

After the redirect, the server queries Kapso to confirm the number belongs to
that client's Kapso customer. Redirect parameters alone never grant ownership.
It saves the customer, number and WABA IDs, visible number and connection state.
The Kapso project key remains in the server environment.

**Verify connection** reconciles the webhook and checks Meta funding separately.
A connected number does not guarantee active funding. Resolve pending,
revoked or unfunded accounts in Kapso and maintain sufficient credits. An
existing Meta webhook pointing elsewhere requires operator review in Kapso.
Coexistence can preserve WhatsApp Business App use where the account supports
it; availability and reliability must be confirmed with the actual pilot.

## Inbox, delivery and consent

Kapso posts signed raw Meta events to
`/api/public/kapso/channels/{channel_id}/webhook`. The API verifies the HMAC,
WABA and phone number, limits payloads to 2 MiB, and persists events before
acknowledgement. Replayed payloads and message IDs are deduplicated. The queue
reuses the existing message, attachment, reaction and delivery-status pipeline.

Taking a conversation into human mode pauses AI replies and bot reminders.
Employees answer in the existing inbox; returning to AI mode resumes automation.
Failed processing can be retried from **Events requiring review**. Interrupted
or ambiguous sends show **Check delivery**: inspect WhatsApp before sending
again. They are not automatically retried.

Record explicit WhatsApp consent and its evidence in the appointment form, or
let the bot ask separately and quote the customer's exact latest message.
An inquiry or appointment confirmation is not itself reminder consent.
Withdrawing consent stops future reminders and proactive Kapso follow-ups.

Inside the reply window, reminders use configured text. Outside it, they require
a currently approved `UTILITY` template. Configure its exact name, language and
body parameters, one value per line in template order. This form handles body
parameters; use a compatible template if header or button values are required.
Text and values support `{name}`, `{appointment}` and `{date}` in the business
timezone. The appointment list shows processing and delivery status.
Cancelled, expired, blocked and human-controlled conversations skip reminders.
Existing inactivity follow-ups stay within their permitted reply window;
approved-template follow-ups can use the existing template webhooks, with
recorded consent for Kapso sends.

## Manual plans and payments

Agency staff assign a name, `GTQ`/`USD` currency, start date, setup/monthly fees,
included outgoing messages, overage price, Kapso send cap and active-user cap.
Suspension stops messaging automation and Kapso sends. User caps prevent new
or reactivated users over the limit; they do not delete existing users.

Record payments with a unique reference and billing month. An identical repeated
reference is idempotent; conflicting details are rejected. Currency cannot
change after payments exist. No payment gateway or card charges are enabled.

Monthly charges and balances are estimates using the current plan and business
timezone. Changing the plan also changes prior estimates. The setup fee applies
in its starting month. Historical invoices are not frozen and tax invoices
are not issued. Billing counts accepted outgoing WhatsApp message IDs,
including human replies and templates. Failed/ambiguous attempts reserve send
quota conservatively; reservations are displayed separately. Reconcile Kapso,
Meta, hosting and AI costs against their invoices. Reported AI cost is in USD.

## Pilot acceptance

The intended pilot is `+502 5309 4054`. No real connection or message exchange
was performed while preparing this release: server credentials and public
HTTPS remain pending. Once configured, verify with authorized test contacts:

- Hosted setup, customer/number ownership, webhook readiness and funding.
- Reception, a reply, an attachment and its delivery receipt in the CRM.
- One lead and reply when the same event is replayed.
- A valid booking; a competing booking must fail without overlapping.
- Employee takeover stops the bot.
- A consented reminder uses an approved template outside the reply window;
  withdrawal of consent skips a subsequent reminder.
- The message cap works and recording a payment changes the balance once.

Complete this pilot and deployment checks before onboarding paying clients.

## Provider references

- [Customer onboarding](https://docs.kapso.ai/docs/platform/customer-guide)
- [Kapso-managed Meta app](https://docs.kapso.ai/docs/platform/tech-providers/overview)
- [Setup links](https://docs.kapso.ai/docs/platform/setup-links/manage)
- [Pricing](https://kapso.com/pricing) and [terms](https://kapso.com/terms)
