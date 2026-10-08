"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { LoaderCircle } from "lucide-react";
import { api, messageFrom } from "@/lib/api";
import { useLanguage } from "@/lib/i18n";
import { Alert } from "@/components/ui";

type Plan = { plan_name: string; status: "active" | "suspended"; currency: "GTQ" | "USD"; starts_on: string;
  setup_cents: number; monthly_cents: number; included_messages: number; overage_cents: number; message_limit: number | null; max_users: number | null };
type Summary = { config: Plan; period: string; messages: number; quota_reserved: number; overage_messages: number; due_cents: number; paid_cents: number; balance_cents: number;
  ai_cost_usd: number; payments: { id: string; amount_cents: number; reference: string; currency: string; created_at: string }[] };

export function BillingView({ base }: { base: string }) {
  const { t, lang } = useLanguage();
  const [summary, setSummary] = useState<Summary | null>(null);
  const [period, setPeriod] = useState(() => new Date().toISOString().slice(0, 7));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const load = useCallback(async () => { setSummary(await api<Summary>(`${base}/billing?period=${period}`)); }, [base, period]);
  useEffect(() => { load().catch((err) => setError(messageFrom(err))); }, [load]);
  async function savePlan(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const data = new FormData(event.currentTarget);
    const cents = (key: string) => Math.round(Number(data.get(key)) * 100);
    setBusy(true); setError("");
    try {
      await api(`${base}/billing`, { method: "PUT", body: JSON.stringify({ plan_name: data.get("plan_name"), status: data.get("status"), currency: data.get("currency"), starts_on: data.get("starts_on"),
        setup_cents: cents("setup"), monthly_cents: cents("monthly"), included_messages: Number(data.get("included")), overage_cents: cents("overage"),
        message_limit: data.get("limit") ? Number(data.get("limit")) : null, max_users: data.get("max_users") ? Number(data.get("max_users")) : null }) });
      await load();
    } catch (err) { setError(messageFrom(err)); } finally { setBusy(false); }
  }
  async function recordPayment(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const form = event.currentTarget; const data = new FormData(form);
    setBusy(true); setError("");
    try {
      await api(`${base}/billing/payments`, { method: "POST", body: JSON.stringify({ amount_cents: Math.round(Number(data.get("amount")) * 100), period, reference: data.get("reference") }) });
      form.reset(); await load();
    } catch (err) { setError(messageFrom(err)); } finally { setBusy(false); }
  }
  if (!summary) return error ? <Alert>{error}</Alert> : <p>{t("crm.loading")}</p>;
  const config = summary.config;
  const money = (cents: number, currency = config.currency) => new Intl.NumberFormat(lang, { style: "currency", currency }).format(cents / 100);
  return <div className="crm-billing">
    {error && <Alert>{error}</Alert>}
    <section className="crm-panel"><div className="crm-section-head"><h2>{t("crm.billing")}</h2><label>{t("crm.period")}<input type="month" required value={period} onChange={(event) => event.target.value && setPeriod(event.target.value)} /></label></div>
      <div className="crm-metrics">{([['messages', summary.messages], ['quota', summary.quota_reserved], ['overageMessages', summary.overage_messages], ['due', money(summary.due_cents)], ['paid', money(summary.paid_cents)], ['balance', money(summary.balance_cents)], ['aiCost', new Intl.NumberFormat(lang, { style: "currency", currency: "USD", maximumFractionDigits: 4 }).format(summary.ai_cost_usd)]] as const).map(([label, value]) => <div key={label}><small>{t(`crm.${label}`)}</small><strong>{value}</strong></div>)}</div><p className="crm-hint">{t("crm.usageHint")}</p>
    </section>
    <form className="crm-panel crm-grid" onSubmit={savePlan} key={JSON.stringify(config)}>
      <label>{t("crm.plan")}<input name="plan_name" maxLength={80} defaultValue={config.plan_name} /></label><label>{t("crm.status")}<select name="status" defaultValue={config.status}><option value="active">{t("crm.active")}</option><option value="suspended">{t("crm.suspended")}</option></select></label>
      <label>{t("crm.currency")}<select name="currency" defaultValue={config.currency}><option>GTQ</option><option>USD</option></select></label><label>{t("crm.startsOn")}<input name="starts_on" type="date" required defaultValue={config.starts_on} /></label>
      <label>{t("crm.setupFee")}<input name="setup" type="number" step="0.01" min={0} max={1000000} required defaultValue={config.setup_cents / 100} /></label><label>{t("crm.monthlyFee")}<input name="monthly" type="number" step="0.01" min={0} max={1000000} required defaultValue={config.monthly_cents / 100} /></label>
      <label>{t("crm.included")}<input name="included" type="number" min={0} max={10000000} required defaultValue={config.included_messages} /></label><label>{t("crm.overage")}<input name="overage" type="number" step="0.01" min={0} max={1000} required defaultValue={config.overage_cents / 100} /></label>
      <label>{t("crm.limit")}<input name="limit" type="number" min={1} max={10000000} defaultValue={config.message_limit ?? ""} /></label><label>{t("crm.maxUsers")}<input name="max_users" type="number" min={1} max={10000} defaultValue={config.max_users ?? ""} /></label>
      <div className="crm-form-actions"><button className="button primary" disabled={busy}>{busy && <LoaderCircle className="spin" size={16} />}{t("crm.save")}</button></div>
    </form>
    <section className="crm-panel"><h3>{t("crm.recordPayment")}</h3><form className="crm-grid" onSubmit={recordPayment}>
      <label>{t("crm.amount")} ({config.currency})<input type="number" name="amount" required min="0.01" max={1000000} step="0.01" /></label><label>{t("crm.reference")}<input name="reference" required maxLength={240} /></label><div className="crm-form-actions"><button className="button primary" disabled={busy}>{t("crm.recordPayment")}</button></div>
    </form><h3>{t("crm.paymentHistory")}</h3>{!summary.payments.length && <p>{t("crm.noPayments")}</p>}<div className="crm-line-list">{summary.payments.map((payment) => <article key={payment.id}><div><strong>{payment.reference}</strong><small>{new Date(payment.created_at).toLocaleDateString(lang)}</small></div><strong>{money(payment.amount_cents, payment.currency as "GTQ" | "USD")}</strong></article>)}</div></section>
  </div>;
}
