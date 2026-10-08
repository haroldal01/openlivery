"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { CalendarDays, LoaderCircle, Plus, RefreshCw } from "lucide-react";
import { api, messageFrom } from "@/lib/api";
import { useLanguage, type I18nKey } from "@/lib/i18n";
import { Alert, Modal } from "@/components/ui";
import { KapsoConnection } from "@/components/kapso-connection";
import { BillingView } from "@/components/manual-billing";
import type { WhatsAppCloudChannel } from "@/types";

const STAGES = ["new", "qualified", "proposal", "won", "lost"] as const;
type Stage = typeof STAGES[number];
type CRMSettings = {
  business_faq: string; auto_leads: boolean; appointments_enabled: boolean; resources: string[]; weekdays: number[];
  opens_at: string; closes_at: string; slot_minutes: number; reminder_minutes: number;
  reminder_text: string; reminder_template: string; reminder_language: string; reminder_parameters: string[];
};
type Contact = { id: string; name: string; phone: string | null; whatsapp_opt_in_at: string | null };
type Opportunity = { id: string; contact_id: string | null; title: string; stage: Stage; value_cents: number; notes: string; conversation_id: string | null };
type Appointment = { id: string; title: string; resource: string; starts_at: string; status: "confirmed" | "cancelled"; reminder_status: string; reminder_error: string | null; delivery_status: string | null };
type Slot = { starts_at: string; ends_at: string };
type Conversation = { id: string; title: string; mode: string; status: string };
type Workspace = { settings: CRMSettings; timezone: string; contacts: Contact[]; opportunities: Opportunity[]; appointments: Appointment[];
  channels: WhatsAppCloudChannel[]; webhook_errors: { id: string; state: string; error: string; created_at: string }[] };
type View = "pipeline" | "appointments" | "automation" | "whatsapp" | "billing";
const VIEWS: View[] = ["pipeline", "appointments", "automation", "whatsapp", "billing"];
const numberValue = (data: FormData, key: string) => Number(data.get(key) || 0);
const lines = (value: FormDataEntryValue | null) => String(value || "").split("\n").map((item) => item.trim()).filter(Boolean);

export function CrmWorkspace({ base, agency = false, canManage = false, agents: suppliedAgents }: {
  base: string; agency?: boolean; canManage?: boolean; agents?: { id: string; name: string }[];
}) {
  const { t, lang } = useLanguage();
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [agents, setAgents] = useState(suppliedAgents || []);
  const [view, setView] = useState<View>("pipeline");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [modal, setModal] = useState<"opportunity" | "appointment" | null>(null);
  const [editing, setEditing] = useState<Opportunity | null>(null);
  const [contactId, setContactId] = useState("");
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [date, setDate] = useState(() => new Date().toISOString().slice(0, 10));
  const [resource, setResource] = useState("");
  const [slots, setSlots] = useState<Slot[]>([]);
  const [loadingSlots, setLoadingSlots] = useState(false);
  const [slot, setSlot] = useState("");
  const [consentSource, setConsentSource] = useState("");
  const load = useCallback(async () => {
    const value = await api<Workspace>(`${base}/crm`);
    setWorkspace(value);
    return value;
  }, [base]);
  useEffect(() => { load().catch((err) => setError(messageFrom(err))); }, [load]);
  useEffect(() => {
    if (suppliedAgents) return;
    api<{ id: string; name: string }[]>(`${base}/agents`).then(setAgents).catch((err) => setError(messageFrom(err)));
  }, [base, suppliedAgents]);
  useEffect(() => {
    const query = new URLSearchParams(window.location.search);
    if (query.has("setup_link_id")) setView("whatsapp");
  }, []);
  useEffect(() => {
    if (!contactId) { setConversations([]); return; }
    let cancelled = false;
    api<Conversation[]>(`${base}/crm/contacts/${contactId}/conversations`).then((items) => { if (!cancelled) setConversations(items); }).catch((err) => { if (!cancelled) setError(messageFrom(err)); });
    return () => { cancelled = true; };
  }, [base, contactId]);
  useEffect(() => {
    setSlot(""); setSlots([]);
    if (modal !== "appointment" || !resource || !date) return;
    let cancelled = false;
    setLoadingSlots(true);
    api<Slot[]>(`${base}/crm/slots?day=${date}&resource=${encodeURIComponent(resource)}`)
      .then((items) => { if (!cancelled) setSlots(items); })
      .catch((err) => { if (!cancelled) setError(messageFrom(err)); })
      .finally(() => { if (!cancelled) setLoadingSlots(false); });
    return () => { cancelled = true; };
  }, [base, modal, resource, date]);
  async function mutate(path: string, method: string, body?: unknown) {
    setBusy(true); setError("");
    try { await api(`${base}/crm${path}`, { method, body: body === undefined ? undefined : JSON.stringify(body) }); await load(); return true; }
    catch (err) { setError(messageFrom(err)); return false; }
    finally { setBusy(false); }
  }
  async function saveOpportunity(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const data = new FormData(event.currentTarget);
    const payload = { title: data.get("title"), stage: data.get("stage"), notes: data.get("notes"),
      value_cents: Math.round(numberValue(data, "value") * 100), ...(!editing ? { contact_id: data.get("contact_id") || null } : {}) };
    if (await mutate(editing ? `/opportunities/${editing.id}` : "/opportunities", editing ? "PATCH" : "POST", payload)) setModal(null);
  }
  async function saveAppointment(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const data = new FormData(event.currentTarget);
    if (await mutate("/appointments", "POST", { title: data.get("title"), contact_id: contactId, resource,
      starts_at: slot, conversation_id: data.get("conversation_id") || null, reminder: data.get("reminder") === "on" })) setModal(null);
  }
  async function saveSettings(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const data = new FormData(event.currentTarget);
    await mutate("/settings", "PUT", { business_faq: data.get("business_faq"), auto_leads: data.get("auto_leads") === "on",
      appointments_enabled: data.get("appointments_enabled") === "on", resources: lines(data.get("resources")),
      weekdays: data.getAll("weekdays").map(Number), opens_at: data.get("opens_at"), closes_at: data.get("closes_at"),
      slot_minutes: numberValue(data, "slot_minutes"), reminder_minutes: numberValue(data, "reminder_minutes"),
      reminder_text: data.get("reminder_text"), reminder_template: data.get("reminder_template"),
      reminder_language: data.get("reminder_language"), reminder_parameters: lines(data.get("reminder_parameters")) });
  }
  if (!workspace) return <div className="crm-workspace">{error ? <Alert>{error}</Alert> : <p><LoaderCircle size={18} className="spin" /> {t("crm.loading")}</p>}</div>;
  const config = workspace.settings;
  const contact = workspace.contacts.find((item) => item.id === contactId);
  const nameOf = (id: string | null) => workspace.contacts.find((item) => item.id === id)?.name || workspace.contacts.find((item) => item.id === id)?.phone || t("crm.noContact");
  const stamp = (value: string) => new Intl.DateTimeFormat(lang, { dateStyle: "medium", timeStyle: "short", timeZone: workspace.timezone }).format(new Date(value));
  const statusLabel = (status: string) => t((`crm.${status}`) as I18nKey);
  return <div className="crm-workspace">
    <div className="crm-toolbar"><div className="segmented" role="tablist" aria-label={t("crm.title")}>{VIEWS.filter((item) => (agency || item !== "billing") && (canManage || (item !== "automation" && item !== "whatsapp"))).map((item) => <button type="button" role="tab" aria-selected={view === item} className={view === item ? "active" : ""} key={item} onClick={() => setView(item)}>{t(`crm.${item}`)}</button>)}</div><button type="button" className="button secondary" disabled={busy} onClick={() => { setError(""); load().catch((err) => setError(messageFrom(err))); }}><RefreshCw size={15} /> {t("crm.refresh")}</button></div>
    {error && <Alert>{error}</Alert>}
    {view === "pipeline" && <>
      <div className="crm-section-head"><div><h2>{t("crm.pipeline")}</h2><small>{t("crm.latest")}</small></div><button className="button primary" onClick={() => { setEditing(null); setModal("opportunity"); }}><Plus size={16} /> {t("crm.addOpportunity")}</button></div>
      <div className="crm-pipeline">{STAGES.map((stage) => <section key={stage}><header><strong>{t(`crm.${stage}`)}</strong><span>{workspace.opportunities.filter((row) => row.stage === stage).length}</span></header>{workspace.opportunities.filter((row) => row.stage === stage).map((row) => <article key={row.id}><button className="text-button" onClick={() => { setEditing(row); setModal("opportunity"); }}><strong>{row.title}</strong></button><small>{nameOf(row.contact_id)}</small>{row.notes && <p>{row.notes}</p>}<strong>{new Intl.NumberFormat(lang, { style: "currency", currency: "GTQ" }).format(row.value_cents / 100)}</strong><label className="sr-only" htmlFor={`stage-${row.id}`}>{t("crm.stage")}</label><select id={`stage-${row.id}`} value={row.stage} disabled={busy} onChange={(event) => mutate(`/opportunities/${row.id}`, "PATCH", { stage: event.target.value })}>{STAGES.map((value) => <option key={value} value={value}>{t(`crm.${value}`)}</option>)}</select></article>)}</section>)}</div>
    </>}
    {view === "appointments" && <section className="crm-panel"><div className="crm-section-head"><div><h2><CalendarDays size={20} /> {t("crm.appointments")}</h2><p>{t("crm.timezone")}: {workspace.timezone}</p></div><button className="button primary" disabled={!config.appointments_enabled} onClick={() => { setContactId(""); setResource(config.resources[0]); setModal("appointment"); }}><Plus size={16} /> {t("crm.book")}</button></div>
      {!workspace.appointments.length && <p>{t("crm.empty")}</p>}
      <div className="crm-line-list">{workspace.appointments.map((row) => <article key={row.id}><div><strong>{row.title}</strong><small>{stamp(row.starts_at)} · {row.resource} · {statusLabel(row.status)}</small><small>{t("crm.delivery")}: {statusLabel(row.reminder_status)}{row.delivery_status && <> / {statusLabel(row.delivery_status)}</>}</small>{row.reminder_error && <p className="crm-error">{row.reminder_error}</p>}</div>{row.status === "confirmed" && <button className="button secondary" disabled={busy} onClick={() => mutate(`/appointments/${row.id}/cancel`, "POST")}>{t("crm.cancelAppointment")}</button>}</article>)}</div>
    </section>}
    {view === "automation" && canManage && <form className="crm-panel crm-grid" onSubmit={saveSettings}>
      <label className="crm-wide">{t("crm.faq")}<textarea name="business_faq" rows={6} maxLength={10000} defaultValue={config.business_faq} /><small>{t("crm.faqHint")}</small></label>
      <label className="crm-check"><input type="checkbox" name="auto_leads" defaultChecked={config.auto_leads} /> {t("crm.autoLeads")}</label><label className="crm-check"><input type="checkbox" name="appointments_enabled" defaultChecked={config.appointments_enabled} /> {t("crm.enableAppointments")}</label>
      <label>{t("crm.resources")}<textarea name="resources" rows={3} defaultValue={config.resources.join("\n")} required /></label>
      <fieldset><legend>{t("crm.weekdays")}</legend>{[0, 1, 2, 3, 4, 5, 6].map((day) => <label key={day} className="crm-check"><input type="checkbox" name="weekdays" value={day} defaultChecked={config.weekdays.includes(day)} /> {t(`crm.days.d${day}` as I18nKey)}</label>)}</fieldset>
      <label>{t("crm.opens")}<input name="opens_at" type="time" required defaultValue={config.opens_at} /></label><label>{t("crm.closes")}<input name="closes_at" type="time" required defaultValue={config.closes_at} /></label>
      <label>{t("crm.duration")}<input name="slot_minutes" type="number" min={15} max={240} required defaultValue={config.slot_minutes} /></label><label>{t("crm.reminderMinutes")}<input name="reminder_minutes" type="number" min={0} max={43200} required defaultValue={config.reminder_minutes} /></label>
      <label className="crm-wide">{t("crm.reminderText")}<textarea name="reminder_text" rows={3} maxLength={4000} defaultValue={config.reminder_text} /></label>
      <label>{t("crm.reminderTemplate")}<input name="reminder_template" pattern="[a-z0-9_]*" maxLength={512} defaultValue={config.reminder_template} /></label><label>{t("crm.templateLanguage")}<input name="reminder_language" maxLength={10} required defaultValue={config.reminder_language} /></label>
      <label className="crm-wide">{t("crm.templateParameters")}<textarea name="reminder_parameters" rows={3} defaultValue={config.reminder_parameters.join("\n")} /><small>{t("crm.reminderHint")}</small></label>
      <div className="crm-form-actions"><button className="button primary" disabled={busy}>{busy ? <LoaderCircle size={16} className="spin" /> : null}{t("crm.save")}</button></div>
    </form>}
    {view === "whatsapp" && canManage && <><KapsoConnection key={base} base={base} agents={agents} channels={workspace.channels} onRefresh={load} agency={agency} />{agency && <p className="crm-hint">{t("crm.pilotHint")}</p>}</>}
    {view === "billing" && agency && <BillingView base={base} />}
    {canManage && workspace.webhook_errors.length > 0 && <section className="crm-panel"><h3>{t("crm.errors")}</h3>{workspace.webhook_errors.map((event) => <p key={event.id}>{stamp(event.created_at)} · {statusLabel(event.state)} · {event.error} {event.state === "failed" && <button className="button secondary" disabled={busy} onClick={() => mutate(`/webhook-events/${event.id}/retry`, "POST")}>{t("crm.retry")}</button>}</p>)}</section>}
    <Modal open={modal === "opportunity"} title={t("crm.addOpportunity")} onClose={() => setModal(null)}><form className="crm-grid" onSubmit={saveOpportunity} key={editing?.id || "new"}>
      <label className="crm-wide">{t("crm.titleLabel")}<input name="title" required maxLength={240} defaultValue={editing?.title} /></label>
      {!editing && <label>{t("crm.contact")}<select name="contact_id"><option value="">{t("crm.noContact")}</option>{workspace.contacts.map((item) => <option key={item.id} value={item.id}>{item.name || item.phone}</option>)}</select></label>}
      <label>{t("crm.stage")}<select name="stage" defaultValue={editing?.stage || "new"}>{STAGES.map((stage) => <option key={stage} value={stage}>{t(`crm.${stage}`)}</option>)}</select></label>
      <label>{t("crm.value")}<input name="value" type="number" min={0} max={10000000} step="0.01" defaultValue={(editing?.value_cents || 0) / 100} /></label>
      <label className="crm-wide">{t("crm.notes")}<textarea name="notes" maxLength={10000} rows={4} defaultValue={editing?.notes} /></label><div className="crm-form-actions"><button className="button primary" disabled={busy}>{t("crm.save")}</button></div>
    </form></Modal>
    <Modal open={modal === "appointment"} title={t("crm.book")} onClose={() => setModal(null)}><form className="crm-grid" onSubmit={saveAppointment}>
      <p className="crm-wide">{t("crm.timezone")}: {workspace.timezone}</p>
      <label className="crm-wide">{t("crm.titleLabel")}<input name="title" required maxLength={240} /></label>
      <label>{t("crm.contact")}<select required value={contactId} onChange={(event) => { setContactId(event.target.value); setConsentSource(""); }}><option value="">{t("crm.noContact")}</option>{workspace.contacts.map((item) => <option key={item.id} value={item.id}>{item.name || item.phone}</option>)}</select></label>
      <label>{t("crm.conversation")}<select name="conversation_id" key={contactId}><option value="">{t("crm.noConversation")}</option>{conversations.map((item) => <option key={item.id} value={item.id}>{item.title} · {item.mode}</option>)}</select></label>
      {contact && <fieldset className="crm-wide"><legend>{t("crm.consent")}</legend><small>{t("crm.consentHint")}</small>{contact.whatsapp_opt_in_at ? <div className="crm-form-actions"><span>{stamp(contact.whatsapp_opt_in_at)}</span><button type="button" className="button secondary" disabled={busy} onClick={() => mutate(`/contacts/${contactId}/consent`, "PUT", { consent: false, source: "Withdrawn by CRM operator" })}>{t("crm.revokeConsent")}</button></div> : <><label>{t("crm.consentSource")}<input maxLength={240} value={consentSource} onChange={(event) => setConsentSource(event.target.value)} /></label><button type="button" className="button secondary" disabled={busy || !consentSource.trim()} onClick={() => mutate(`/contacts/${contactId}/consent`, "PUT", { consent: true, source: consentSource })}>{t("crm.recordConsent")}</button></>}</fieldset>}
      <label>{t("crm.date")}<input type="date" required value={date} onChange={(event) => setDate(event.target.value)} /></label>
      <label>{t("crm.resource")}<select required value={resource} onChange={(event) => setResource(event.target.value)}>{config.resources.map((name) => <option key={name} value={name}>{name}</option>)}</select></label>
      <label className="crm-wide">{t("crm.slot")}<select required value={slot} disabled={loadingSlots} onChange={(event) => setSlot(event.target.value)}><option value="">{loadingSlots ? t("crm.loading") : t("crm.noSlots")}</option>{slots.map((item) => <option key={item.starts_at} value={item.starts_at}>{stamp(item.starts_at)}</option>)}</select></label>
      <label className="crm-check crm-wide"><input name="reminder" type="checkbox" disabled={!contact?.whatsapp_opt_in_at} /> {t("crm.reminder")}</label>
      <div className="crm-form-actions"><button className="button primary" disabled={busy || !slot || !contactId}>{t("crm.book")}</button></div>
    </form></Modal>
  </div>;
}
