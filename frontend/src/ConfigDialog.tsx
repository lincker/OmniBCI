/** HTML dialogs also work in desktop webviews without native prompt support. */
import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { useI18n } from "./i18n";

export type ConfigDialogProps = {
  title: string;
  message: string;
  details?: ReactNode;
  confirmLabel: string;
  cancelLabel?: string | null;
  initialValue?: string;
  onConfirm: (value: string) => void;
  onCancel: () => void;
};

export function ConfigDialog({ title, message, details, confirmLabel, cancelLabel, initialValue, onConfirm, onCancel }: ConfigDialogProps) {
  const { t } = useI18n();
  const dialog = useRef<HTMLDialogElement>(null);
  const previousFocus = useRef(typeof document === "undefined" ? null : document.activeElement as HTMLElement | null);
  const titleId = useId();
  const messageId = useId();
  const [value, setValue] = useState(initialValue ?? "");
  useEffect(() => {
    const current = dialog.current;
    current?.showModal();
    return () => {
      current?.close();
      if (previousFocus.current?.isConnected) previousFocus.current.focus();
    };
  }, []);
  return (
    <dialog className="config-dialog" ref={dialog} aria-labelledby={titleId} aria-describedby={messageId}
      onCancel={(event) => { event.preventDefault(); onCancel(); }}>
      <form onSubmit={(event) => { event.preventDefault(); onConfirm(value); }}>
        <h3 id={titleId}>{title}</h3>
        <p id={messageId}>{message}</p>
        {details}
        {initialValue !== undefined && <label>
          {t("dialog.newProfileName")}
          <input autoFocus required maxLength={64} value={value} onChange={(event) => setValue(event.target.value)} />
        </label>}
        <div className="actions">
          {cancelLabel !== null && <button type="button" autoFocus={initialValue === undefined} onClick={onCancel}>{cancelLabel ?? t("dialog.cancel")}</button>}
          <button type="submit" className="primary" autoFocus={cancelLabel === null && initialValue === undefined}>{confirmLabel}</button>
        </div>
      </form>
    </dialog>
  );
}
