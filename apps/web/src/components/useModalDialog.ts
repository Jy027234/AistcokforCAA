import { useEffect, useRef } from "react";

const FOCUSABLE_SELECTOR = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

/** Adds the keyboard and focus behavior shared by the app's modal drawers. */
export function useModalDialog(dialogRef: React.RefObject<HTMLElement>, onClose: () => void) {
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    const activeDialog = dialog;

    const ownerDocument = activeDialog.ownerDocument;
    const opener = ownerDocument.activeElement;
    const focusableElements = () => Array.from(
      activeDialog.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR),
    ).filter((element) => element.getAttribute("aria-hidden") !== "true");

    (focusableElements()[0] ?? activeDialog).focus();

    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        onCloseRef.current();
        return;
      }
      if (event.key !== "Tab") return;

      const focusable = focusableElements();
      if (focusable.length === 0) {
        event.preventDefault();
        activeDialog.focus();
        return;
      }

      const first = focusable[0]!;
      const last = focusable[focusable.length - 1]!;
      const active = ownerDocument.activeElement;
      if (event.shiftKey && (active === first || !activeDialog.contains(active))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (active === last || !activeDialog.contains(active))) {
        event.preventDefault();
        first.focus();
      }
    }

    ownerDocument.addEventListener("keydown", onKeyDown);
    return () => {
      ownerDocument.removeEventListener("keydown", onKeyDown);
      if (opener && opener.isConnected) (opener as HTMLElement).focus();
    };
  }, [dialogRef]);
}
