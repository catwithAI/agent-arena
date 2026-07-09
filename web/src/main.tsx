import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, NavLink, Route, Routes } from "react-router-dom";

import { Submit } from "./pages/Submit";
import { RunList } from "./pages/RunList";
import { RunDetail } from "./pages/RunDetail";
import { Scenarios } from "./pages/Scenarios";
import "./styles.css";

function App() {
  return (
    <BrowserRouter>
      <header>
        <h1>agent-lane</h1>
        <nav>
          <NavLink to="/" end>
            New run
          </NavLink>
          <NavLink to="/runs">History</NavLink>
          <NavLink to="/scenarios">Scenarios &amp; scoring</NavLink>
        </nav>
      </header>
      <main>
        <Routes>
          <Route path="/" element={<Submit />} />
          <Route path="/runs" element={<RunList />} />
          <Route path="/runs/:runId" element={<RunDetail />} />
          <Route path="/scenarios" element={<Scenarios />} />
        </Routes>
      </main>
    </BrowserRouter>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
