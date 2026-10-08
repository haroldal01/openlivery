"use client";

import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { CheckCircle2, LoaderCircle, Plug, RefreshCw } from "lucide-react";
import { api, messageFrom } from "@/lib/api";
import { useLanguage, type I18nKey } from "@/lib/i18n";
import { Alert } from "@/components/ui";
import type { WhatsAppCloudChannel } from "@/types";

type AgentChoice = { id: string; name: string };
type KapsoNumber = { phone_number_id: string; display_phone_number: string | null; display_name: string | null; status: string | null; linked: boolean };
type KapsoNumbers = { data: KapsoNumber[]; next_page: number | null };
export function KapsoConnection({ base, agents, channels, onRefresh, agency = false }: {
  base: string; agents: AgentChoice[]; channels: WhatsAppCloudChannel[]; onRefresh: () => Promise<unknown>; agency?: boolean;
}) {
  const { t, lang } = useLanguage();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [mode, setMode] = useState<"setup" | "existing">("setup");
  const [numbers, setNumbers] = useState<KapsoNumbers | null>(null);
  const [page, setPage] = useState(1);
  const [phone, setPhone] = useState("");
  const checked = useRef(false);
  const lines = channels.filter((line) => line.provider === "kapso");
  const verify = useCallback(async (id: string, phone?: string | null) => {
    setBusy(true); setError("");
    try {
      await api(`${base}/kapso/channels/${id}/refresh`, { method: "POST", body: JSON.stringify({ phone_number_id: phone || undefined }) });
      await onRefresh();
    } catch (err) { setError(messageFrom(err)); } finally { setBusy(false); }
  }, [base, onRefresh]);
  useEffect(() => {
    if (checked.current) return;
    const query = new URLSearchParams(window.location.search);
    const setupId = query.get("setup_link_id");
    const line = channels.find((item) => item.kapso_setup_link_id === setupId);
    if (!setupId || !line) return;
    checked.current = true;
    if (query.get("error_code")) setError(t("crm.setupFailure"));
    else void verify(line.id, query.get("phone_number_id"));
  }, [channels, verify, t]);
  async function connect(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setError("");
    const data = new FormData(event.currentTarget);
    try {
      const result = await api<{ url: string }>(`${base}/kapso/setup-link`, { method: "POST", body: JSON.stringify({
        agent_id: data.get("agent_id"), label: data.get("label"), language: lang,
        channel_id: data.get("channel_id") || undefined,
      }) });
      window.location.assign(result.url);
    } catch (err) { setError(messageFrom(err)); setBusy(false); }
  }
  async function loadNumbers(nextPage = 1) {
    setBusy(true); setError(""); setNotice(""); setPhone("");
    try {
      const result = await api<KapsoNumbers>(`${base}/kapso/phone-numbers?page=${nextPage}`);
      setNumbers(result); setPage(nextPage);
    } catch (err) { setNumbers(null); setError(messageFrom(err)); }
    finally { setBusy(false); }
  }
  async function importNumber(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setError(""); setNotice("");
    const data = new FormData(event.currentTarget);
    try {
      const saved = await api<WhatsAppCloudChannel>(`${base}/kapso/import`, { method: "POST", body: JSON.stringify({
        phone_number_id: phone, agent_id: data.get("agent_id"), label: data.get("label"),
      }) });
      await onRefresh();
      setNotice(t(saved.status === "connected" && saved.kapso_webhook_id ? "crm.imported" : "crm.importedPending"));
    } catch (err) {
      setError(messageFrom(err));
      // A saved line remains visible if webhook registration needs a retry.
      await onRefresh().catch(() => undefined);
    } finally { setBusy(false); }
  }
  return <section className="crm-panel">
    <div className="crm-section-head"><div><h2><Plug size={20} /> {t("crm.connect")}</h2><p>{t("crm.connectHint")}</p></div></div>
    {error && <Alert>{error}</Alert>}
    {notice && <p role="status">{notice}</p>}
    <p className="crm-hint">{t("crm.billingDisclosure")}</p>
    <div className="crm-line-list">{lines.map((line) => <article key={line.id}>
      <div><strong>{line.label || line.phone_number || t("crm.whatsapp")}</strong><small>{line.phone_number || "—"} · {line.status === "connected" ? t("crm.configured") : t("crm.disconnected")} · {line.kapso_webhook_id ? t("crm.webhookReady") : t("crm.webhookPending")}{line.coexistence ? " · WhatsApp Business" : ""}</small><small>{t("crm.fundingLabel")}: {t(`crm.funding.${["funded", "pending", "not_funded", "revoked"].includes(line.kapso_funding?.status || "") ? line.kapso_funding.status : "unknown"}` as I18nKey)}{line.kapso_funding?.paid_messages_paused && <> · {t("crm.suspended")}</>}</small>{line.last_error && <Alert>{line.last_error}</Alert>}</div>
      <button type="button" className="button secondary" disabled={busy} onClick={() => verify(line.id)}><RefreshCw size={15} /> {t("crm.verify")}</button>
    </article>)}</div>
    {agency && <div className="crm-form-actions">
      <button type="button" className={`button ${mode === "setup" ? "primary" : "secondary"}`} disabled={busy} aria-pressed={mode === "setup"} onClick={() => { setMode("setup"); setError(""); setNotice(""); }}>{t("crm.newConnection")}</button>
      <button type="button" className={`button ${mode === "existing" ? "primary" : "secondary"}`} disabled={busy} aria-pressed={mode === "existing"} onClick={() => { setMode("existing"); void loadNumbers(); }}>{t("crm.useExisting")}</button>
    </div>}
    {agency && mode === "existing" ? <>
      <p className="crm-hint">{t("crm.importHint")}</p>
      <div className="crm-form-actions">
        <button type="button" className="button secondary" disabled={busy} onClick={() => loadNumbers(page)}><RefreshCw size={15} /> {t("crm.loadNumbers")}</button>
        {numbers && page > 1 && <button type="button" className="button secondary" disabled={busy} onClick={() => loadNumbers(page - 1)}>{t("crm.previousNumbers")}</button>}
        {numbers?.next_page && <button type="button" className="button secondary" disabled={busy} onClick={() => loadNumbers(numbers.next_page!)}>{t("crm.nextNumbers")}</button>}
      </div>
      {numbers && !numbers.data.length && <p role="status">{t("crm.noKapsoNumbers")}</p>}
      <form className="crm-grid" onSubmit={importNumber}>
        <label className="crm-wide">{t("crm.existingNumber")}<select name="phone_number_id" required value={phone} disabled={busy || !numbers?.data.length} onChange={(event) => setPhone(event.target.value)}>
          <option value="">{t("crm.selectNumber")}</option>
          {numbers?.data.map((number) => <option key={number.phone_number_id} value={number.phone_number_id}>{number.display_phone_number || number.phone_number_id}{number.display_name ? ` · ${number.display_name}` : ""} · {t(number.status === "CONNECTED" ? "crm.configured" : "crm.disconnected")}{number.linked ? ` · ${t("crm.alreadyLinked")}` : ""}</option>)}
        </select></label>
        <label>{t("crm.agent")}<select name="agent_id" required disabled={busy || !agents.length}>{agents.map((agent) => <option key={agent.id} value={agent.id}>{agent.name}</option>)}</select></label>
        <label>{t("crm.label")}<input name="label" maxLength={80} disabled={busy} /></label>
        {!agents.length && <Alert>{t("crm.noAgents")}</Alert>}
        <div className="crm-form-actions"><button type="submit" className="button primary" disabled={busy || !phone || !agents.length}>{busy ? <LoaderCircle size={16} className="spin" /> : <CheckCircle2 size={16} />} {t("crm.linkNumber")}</button></div>
      </form>
    </> : <form className="crm-grid" onSubmit={connect}>
      {lines.length > 0 && <label>{t("crm.whatsapp")}<select name="channel_id" disabled={busy}><option value="">{t("clients.whatsappCloud.newNumber")}</option>{lines.map((line) => <option key={line.id} value={line.id}>{line.label || line.phone_number || line.id}</option>)}</select></label>}
      <label>{t("crm.agent")}<select name="agent_id" required disabled={busy || !agents.length}>{agents.map((agent) => <option key={agent.id} value={agent.id}>{agent.name}</option>)}</select></label>
      <label>{t("crm.label")}<input name="label" maxLength={80} disabled={busy} /></label>
      {!agents.length && <Alert>{t("crm.noAgents")}</Alert>}
      <div className="crm-form-actions"><button className="button primary" disabled={busy || !agents.length}>{busy ? <LoaderCircle size={16} className="spin" /> : <CheckCircle2 size={16} />} {t("crm.openSetup")}</button></div>
    </form>}
  </section>;
}
