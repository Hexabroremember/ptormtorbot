import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import AdminPanel from "./AdminPanel.jsx";
import App from "./App.jsx";
import "./index.css";

const Root = window.location.pathname.startsWith("/admin") ? AdminPanel : App;

createRoot(document.getElementById("root")).render(
  <StrictMode>
    <Root />
  </StrictMode>,
);
