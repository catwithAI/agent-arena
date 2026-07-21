import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, NavLink, Route, Routes } from "react-router-dom";

import { Submit } from "./pages/Submit";
import { SameModelSubmit } from "./pages/SameModelSubmit";
import { MultiModelSubmit } from "./pages/MultiModelSubmit";
import { RunList } from "./pages/RunList";
import { RunDetail } from "./pages/RunDetail";
import { Scenarios } from "./pages/Scenarios";
import "./styles.css";

function App() {
  return (
    <BrowserRouter>
      <header>
        <div className="brand">
          <span className="brand-glyph">▣</span>
          <span className="brand-mark">
            agent<span className="dim">-</span>arena
          </span>
        </div>
        <nav>
          <NavLink to="/" end>
            新建评测
          </NavLink>
          <NavLink to="/same-model">同模型对比</NavLink>
          <NavLink to="/multi-model">多模型对比</NavLink>
          <NavLink to="/runs">历史记录</NavLink>
          <NavLink to="/scenarios">场景与评分</NavLink>
        </nav>
      </header>
      <main>
        <Routes>
          <Route path="/" element={<Submit />} />
          <Route path="/same-model" element={<SameModelSubmit />} />
          <Route path="/multi-model" element={<MultiModelSubmit />} />
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
