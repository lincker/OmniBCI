import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import DesktopApp from "./DesktopApp";
import { I18nProvider } from "./i18n";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <I18nProvider>
      <DesktopApp />
    </I18nProvider>
  </StrictMode>,
);
