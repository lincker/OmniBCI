/** HTML dialogs also work in desktop webviews without native prompt support. */
import { useEffect, useId, useRef, useState } from "react";
import { useI18n } from "./i18n";

export type ConfigDialogProps = {
  title: string;
  message: string;
  confirmLabel: string;
  initialValue?: string;
  onConfirm: (value: string) => void;
  onCancel: () => void;
};

export function ConfigDialog({ title, message, confirmLabel, initialValue, onConfirm, onCancel }: ConfigDialogProps) {
  const { t } = useI18n();
  const dialog = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const [value, setValue] = useState(initialValue ?? "");
  useEffect(() => { dialog.current?.showModal(); }, []);
  return (
    <dialog className="config-dialog" ref={dialog} aria-labelledby={titleId}
      onCancel={(event) => { event.preventDefault(); onCancel(); }}>
      <form onSubmit={(event) => { event.preventDefault(); onConfirm(value); }}>
        <h3 id={titleId}>{title}</h3>
        <p>{message}</p>
        {initialValue !== undefined && <label>
          {t("dialog.newProfileName")}
          <input autoFocus required maxLength={64} value={value} onChange={(event) => setValue(event.target.value)} />
        </label>}
        <div className="actions">
          <button type="button" onClick={onCancel}>{t("dialog.cancel")}</button>
          <button type="submit" className="primary">{confirmLabel}</button>
        </div>
      </form>
    </dialog>
  );
}
