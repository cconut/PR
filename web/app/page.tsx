"use client";

import {
  Activity,
  ArrowRight,
  Bot,
  Boxes,
  CheckCircle2,
  ChevronRight,
  CircleAlert,
  Clock3,
  Code2,
  ExternalLink,
  FileSearch,
  Gauge,
  GitBranch,
  GitPullRequest,
  History,
  KeyRound,
  ListChecks,
  LoaderCircle,
  LogOut,
  Menu,
  Play,
  Plus,
  RefreshCw,
  RotateCcw,
  SearchCode,
  ServerCog,
  ShieldCheck,
  Sparkles,
  TestTube2,
  X,
  XCircle,
  type LucideIcon,
} from "lucide-react";
import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";

type View = "overview" | "review" | "tasks" | "skills" | "evolution" | "github";
type JsonObject = Record<string, unknown>;

type Task = {
  id: string;
  state: string;
  repository: string;
  pull_request?: number | null;
  created_at?: string;
  report?: unknown;
};

type DashboardData = {
  stats: {
    tasks_total?: number;
    tasks_success?: number;
    tasks_failed?: number;
    success_rate?: number;
    unresolved_failure_cases?: number;
    active_skill_versions?: number;
  };
  tasks: Task[];
  queue: string;
  orchestrator: string;
};

type Skill = {
  name: string;
  version: string;
  description?: string;
  source: string;
  sandboxed?: boolean;
};

type FailureCase = {
  id: number;
  task_id: string;
  category: string;
  resolved: boolean;
};

type EvolutionRun = {
  id: string;
  skill_name?: string;
  candidate_version: number;
  decision: string;
  candidate_score: number;
  baseline_score: number;
};

const navItems: Array<{ view: View; label: string; icon: LucideIcon }> = [
  { view: "overview", label: "运行总览", icon: Gauge },
  { view: "review", label: "发起审查", icon: Plus },
  { view: "tasks", label: "任务中心", icon: ListChecks },
  { view: "skills", label: "Skills", icon: Boxes },
  { view: "evolution", label: "演进实验室", icon: TestTube2 },
  { view: "github", label: "GitHub App", icon: GitPullRequest },
];

const titles: Record<View, { title: string; description: string }> = {
  overview: { title: "运行总览", description: "审查吞吐、运行状态与最近活动" },
  review: { title: "发起审查", description: "提交统一 Diff 并进入多 Agent 分析流程" },
  tasks: { title: "任务中心", description: "检查执行状态、Trace 与修复结果" },
  skills: { title: "Skill 注册中心", description: "管理已加载能力和沙箱来源" },
  evolution: { title: "演进实验室", description: "回放失败案例并执行版本门禁" },
  github: { title: "GitHub App", description: "将审查和修复能力接入仓库工作流" },
};

const stateLabels: Record<string, string> = {
  PENDING: "等待中",
  PLANNING: "规划中",
  EXECUTING: "执行中",
  REVIEWING: "汇总中",
  SUCCESS: "已完成",
  FAILED: "失败",
  CANCELLED: "已取消",
};

function formatTime(value?: string) {
  if (!value) return "时间未知";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function formatJson(value: unknown) {
  return JSON.stringify(value, null, 2);
}

function errorMessage(value: unknown) {
  return value instanceof Error ? value.message : "请求失败";
}

function StatusBadge({ state }: { state: string }) {
  const normalized = String(state || "PENDING").toUpperCase();
  return <span className={`status-badge state-${normalized.toLowerCase()}`}>{stateLabels[normalized] || normalized}</span>;
}

function EmptyState({ title, detail }: { title: string; detail?: string }) {
  return (
    <div className="empty-state">
      <FileSearch size={22} strokeWidth={1.7} />
      <strong>{title}</strong>
      {detail ? <span>{detail}</span> : null}
    </div>
  );
}

function LoadingRows({ count = 3 }: { count?: number }) {
  return <div className="loading-rows" aria-label="正在加载">{Array.from({ length: count }, (_, index) => <i key={index} />)}</div>;
}

function TaskTable({ tasks, onOpen, compact = false }: { tasks: Task[]; onOpen: (id: string) => void; compact?: boolean }) {
  if (!tasks.length) return <EmptyState title="还没有审查任务" detail="提交一个 Diff 开始首次审查" />;
  return (
    <div className={`task-table ${compact ? "compact" : ""}`}>
      <div className="table-head" aria-hidden="true">
        <span>仓库 / PR</span><span>创建时间</span><span>状态</span><span />
      </div>
      {tasks.map((task) => (
        <button className="task-row" key={task.id} type="button" onClick={() => onOpen(task.id)}>
          <span className="repo-cell">
            <GitBranch size={16} />
            <span><strong>{task.repository || "未命名仓库"}</strong><small>{task.pull_request ? `PR #${task.pull_request}` : "手动审查"}</small></span>
          </span>
          <time>{formatTime(task.created_at)}</time>
          <StatusBadge state={task.state} />
          <ChevronRight className="row-arrow" size={16} />
        </button>
      ))}
    </div>
  );
}

function SectionHeader({ eyebrow, title, description, action }: {
  eyebrow: string;
  title: string;
  description: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="section-header">
      <div><span className="kicker">{eyebrow}</span><h2>{title}</h2><p>{description}</p></div>
      {action ? <div className="section-action">{action}</div> : null}
    </div>
  );
}

export default function ConsolePage() {
  const [view, setView] = useState<View>("overview");
  const [mobileNav, setMobileNav] = useState(false);
  const [token, setToken] = useState("");
  const [loginOpen, setLoginOpen] = useState(false);
  const [loginError, setLoginError] = useState("");
  const [toast, setToast] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  const [dashboard, setDashboard] = useState<DashboardData | null>(null);
  const [dashboardError, setDashboardError] = useState("");
  const [tasks, setTasks] = useState<Task[]>([]);
  const [tasksLoading, setTasksLoading] = useState(false);
  const [selectedTask, setSelectedTask] = useState<Task | null>(null);
  const [taskDetail, setTaskDetail] = useState<unknown>(null);
  const [taskDetailLoading, setTaskDetailLoading] = useState(false);
  const [skills, setSkills] = useState<Skill[]>([]);
  const [skillsLoading, setSkillsLoading] = useState(false);
  const [skillEvolutionStatus, setSkillEvolutionStatus] = useState<unknown>(null);
  const [skillEvolutionRuns, setSkillEvolutionRuns] = useState<EvolutionRun[]>([]);
  const [skillEvolutionResult, setSkillEvolutionResult] = useState<unknown>(null);
  const [evolutionStatus, setEvolutionStatus] = useState<unknown>(null);
  const [failures, setFailures] = useState<FailureCase[]>([]);
  const [runs, setRuns] = useState<EvolutionRun[]>([]);
  const [evolutionLoading, setEvolutionLoading] = useState(false);
  const [reviewResult, setReviewResult] = useState<unknown>(null);
  const [evolutionResult, setEvolutionResult] = useState<unknown>(null);
  const [busy, setBusy] = useState("");
  const [fixDialogOpen, setFixDialogOpen] = useState(false);
  const [installationId, setInstallationId] = useState("");

  useEffect(() => {
    setToken(localStorage.getItem("evoagent_token") || "");
    const initial = window.location.hash.slice(1) as View;
    if (titles[initial]) setView(initial);
    const onHash = () => {
      const next = window.location.hash.slice(1) as View;
      if (titles[next]) setView(next);
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    document.title = `${titles[view].title} · EvoAgent`;
  }, [view]);

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(""), 2800);
    return () => window.clearTimeout(timer);
  }, [toast]);

  const request = useCallback(async <T,>(path: string, options: RequestInit = {}): Promise<T> => {
    const headers = new Headers(options.headers);
    if (token) headers.set("Authorization", `Bearer ${token}`);
    const response = await fetch(path, { ...options, headers, cache: "no-store" });
    const contentType = response.headers.get("content-type") || "";
    const data: unknown = contentType.includes("json") ? await response.json() : await response.text();
    if (response.status === 401) setLoginOpen(true);
    if (!response.ok) {
      const body = typeof data === "object" && data ? data as JsonObject : {};
      throw new Error(String(body.error || body.detail || data || response.statusText || "请求失败"));
    }
    return data as T;
  }, [token]);

  const loadDashboard = useCallback(async () => {
    setDashboardError("");
    try {
      setDashboard(await request<DashboardData>("/api/dashboard"));
    } catch (error) {
      setDashboardError(errorMessage(error));
    }
  }, [request]);

  const loadTasks = useCallback(async () => {
    setTasksLoading(true);
    try {
      const data = await request<{ tasks: Task[] }>("/api/tasks");
      setTasks(data.tasks || []);
    } catch (error) {
      setToast(errorMessage(error));
    } finally {
      setTasksLoading(false);
    }
  }, [request]);

  const loadSkills = useCallback(async () => {
    setSkillsLoading(true);
    try {
      // Added: load declarative skill evolution beside the existing Skill registry.
      const [data, statusData, runData] = await Promise.all([
        request<{ skills: Skill[] }>("/api/skills"),
        request<unknown>("/v1/skill-evolution/status"),
        request<{ runs: EvolutionRun[] }>("/v1/skill-evolution/runs?limit=5"),
      ]);
      setSkills(data.skills || []);
      setSkillEvolutionStatus(statusData);
      setSkillEvolutionRuns(runData.runs || []);
    } catch (error) {
      setToast(errorMessage(error));
    } finally {
      setSkillsLoading(false);
    }
  }, [request]);

  const loadEvolution = useCallback(async () => {
    setEvolutionLoading(true);
    try {
      const [failureData, statusData, runData] = await Promise.all([
        request<{ cases: FailureCase[] }>("/api/failures"),
        request<unknown>("/v1/evolution/status"),
        request<{ runs: EvolutionRun[] }>("/v1/evolution/runs?limit=5"),
      ]);
      setFailures(failureData.cases || []);
      setEvolutionStatus(statusData);
      setRuns(runData.runs || []);
    } catch (error) {
      setToast(errorMessage(error));
    } finally {
      setEvolutionLoading(false);
    }
  }, [request]);

  useEffect(() => {
    void loadDashboard();
  }, [loadDashboard]);

  useEffect(() => {
    if (view === "tasks") void loadTasks();
    if (view === "skills") void loadSkills();
    if (view === "evolution") void loadEvolution();
  }, [view, loadEvolution, loadSkills, loadTasks]);

  const navigate = (next: View) => {
    setView(next);
    setMobileNav(false);
    window.history.replaceState(null, "", `#${next}`);
    window.scrollTo({ top: 0, behavior: "smooth" });
  };

  const refresh = async () => {
    setRefreshing(true);
    try {
      if (view === "tasks") await loadTasks();
      else if (view === "skills") await loadSkills();
      else if (view === "evolution") await loadEvolution();
      else await loadDashboard();
      setToast("数据已刷新");
    } finally {
      setRefreshing(false);
    }
  };

  const openTask = async (id: string) => {
    navigate("tasks");
    setTaskDetailLoading(true);
    try {
      const detail = await request<Task & JsonObject>(`/v1/tasks/${encodeURIComponent(id)}`);
      setSelectedTask(detail);
      setTaskDetail(detail);
    } catch (error) {
      setTaskDetail({ error: errorMessage(error) });
    } finally {
      setTaskDetailLoading(false);
    }
  };

  const submitReview = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setBusy("review");
    const values = new FormData(event.currentTarget);
    const body: JsonObject = {
      repository: values.get("repository"),
      diff: values.get("diff"),
    };
    if (values.get("pull_request")) body.pull_request = Number(values.get("pull_request"));
    try {
      const result = await request<unknown>(`/v1/reviews${values.get("async") ? "?async=true" : ""}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      setReviewResult(result);
      setToast("审查任务已提交");
      await loadDashboard();
    } catch (error) {
      setReviewResult({ error: errorMessage(error) });
    } finally {
      setBusy("");
    }
  };

  const createFix = async () => {
    if (!selectedTask) return;
    setBusy("fix");
    try {
      const result = await request<unknown>(`/v1/tasks/${encodeURIComponent(selectedTask.id)}/fix`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ installation_id: installationId ? Number(installationId) : null }),
      });
      setTaskDetail(result);
      setFixDialogOpen(false);
      setToast("修复分支已创建");
    } catch (error) {
      setToast(errorMessage(error));
    } finally {
      setBusy("");
    }
  };

  const reloadSkills = async () => {
    setBusy("skills");
    try {
      await request("/v1/skills/reload", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      await loadSkills();
      setToast("Skills 已重新加载");
    } catch (error) {
      setToast(errorMessage(error));
    } finally {
      setBusy("");
    }
  };

  // Added: derive a safe declarative candidate from unresolved review feedback.
  const autoEvolveSkill = async () => {
    setBusy("skill-evolution");
    try {
      const result = await request<unknown>("/v1/skill-evolution/auto", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ skill_name: "evolved-review" }),
      });
      setSkillEvolutionResult(result);
      setToast("Skill evolution completed");
      await loadSkills();
    } catch (error) {
      setSkillEvolutionResult({ error: errorMessage(error) });
    } finally {
      setBusy("");
    }
  };

  // Added: make a previously evaluated skill version active after an operator review.
  const activateSkillVersion = async (skillName: string, version: number) => {
    setBusy(`skill-activate-${version}`);
    try {
      await request(`/v1/skill-evolution/${encodeURIComponent(skillName)}/versions/${version}/activate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      setToast(`Activated ${skillName} v${version}`);
      await loadSkills();
    } catch (error) {
      setToast(errorMessage(error));
    } finally {
      setBusy("");
    }
  };

  const submitEvolution = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setBusy("evolution");
    const values = new FormData(event.currentTarget);
    try {
      const result = await request<unknown>("/v1/evolution/propose", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ skill_name: values.get("skill_name"), prompt: values.get("prompt") }),
      });
      setEvolutionResult(result);
      setToast("版本回放评测已完成");
      await loadEvolution();
    } catch (error) {
      setEvolutionResult({ error: errorMessage(error) });
    } finally {
      setBusy("");
    }
  };

  const autoEvolve = async () => {
    setBusy("auto-evolve");
    try {
      const result = await request<unknown>("/v1/evolution/auto", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ skill_name: "llm-review" }),
      });
      setEvolutionResult(result);
      setToast("反馈候选评测已完成");
      await loadEvolution();
    } catch (error) {
      setEvolutionResult({ error: errorMessage(error) });
    } finally {
      setBusy("");
    }
  };

  const submitLogin = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setBusy("login");
    setLoginError("");
    const values = new FormData(event.currentTarget);
    try {
      const result = await request<{ access_token: string }>("/v1/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          username: values.get("username"),
          password: values.get("password"),
          tenant_id: values.get("tenant_id"),
        }),
      });
      localStorage.setItem("evoagent_token", result.access_token);
      setToken(result.access_token);
      setLoginOpen(false);
      setToast("登录成功");
    } catch (error) {
      setLoginError(errorMessage(error));
    } finally {
      setBusy("");
    }
  };

  const logout = () => {
    localStorage.removeItem("evoagent_token");
    setToken("");
    setLoginOpen(true);
  };

  const stats = dashboard?.stats || {};
  const metricItems = useMemo(() => [
    { label: "总任务", value: stats.tasks_total ?? 0, detail: "累计进入 Harness", icon: ListChecks, tone: "neutral" },
    { label: "已完成", value: stats.tasks_success ?? 0, detail: "通过最终门禁", icon: CheckCircle2, tone: "success" },
    { label: "失败", value: stats.tasks_failed ?? 0, detail: "需要人工介入", icon: XCircle, tone: "danger" },
    { label: "成功率", value: `${Math.round(Number(stats.success_rate || 0) * 100)}%`, detail: "全部运行记录", icon: Activity, tone: "info" },
    { label: "待处理反馈", value: stats.unresolved_failure_cases ?? 0, detail: "未归档失败案例", icon: CircleAlert, tone: "warning" },
    { label: "活跃版本", value: stats.active_skill_versions ?? 0, detail: "已激活 Skill", icon: Boxes, tone: "violet" },
  ], [stats]);

  return (
    <div className="console-shell">
      <aside className={`sidebar ${mobileNav ? "open" : ""}`}>
        <div className="brand-block">
          <span className="brand-symbol"><Code2 size={19} /></span>
          <span><strong>EvoAgent</strong><small>PR CONTROL PLANE</small></span>
          <button className="mobile-close" onClick={() => setMobileNav(false)} aria-label="关闭导航"><X size={18} /></button>
        </div>
        <nav className="primary-nav" aria-label="主导航">
          <span className="nav-label">WORKSPACE</span>
          {navItems.map(({ view: itemView, label, icon: Icon }) => (
            <button key={itemView} className={view === itemView ? "active" : ""} onClick={() => navigate(itemView)} aria-current={view === itemView ? "page" : undefined}>
              <Icon size={17} strokeWidth={1.8} /><span>{label}</span>
            </button>
          ))}
        </nav>
        <div className="runtime-card">
          <span className={`runtime-dot ${dashboardError ? "offline" : ""}`} />
          <span><small>RUNTIME</small><strong>{dashboardError ? "连接异常" : dashboard ? "运行正常" : "正在连接"}</strong></span>
          <ServerCog size={17} />
        </div>
        <div className="sidebar-meta">
          <span>{dashboard?.queue || "queue pending"}</span>
          <span>{dashboard?.orchestrator || "orchestrator pending"}</span>
        </div>
      </aside>

      {mobileNav ? <button className="nav-scrim" aria-label="关闭导航" onClick={() => setMobileNav(false)} /> : null}

      <main className="workspace">
        <header className="workspace-header">
          <div className="header-title">
            <button className="mobile-menu" onClick={() => setMobileNav(true)} aria-label="打开导航"><Menu size={20} /></button>
            <div><span className="kicker">EVOAGENT / {view.toUpperCase()}</span><h1>{titles[view].title}</h1><p>{titles[view].description}</p></div>
          </div>
          <div className="header-actions">
            <button className="icon-button" onClick={() => void refresh()} disabled={refreshing} title="刷新当前数据" aria-label="刷新当前数据">
              <RefreshCw className={refreshing ? "spin" : ""} size={17} />
            </button>
            {token ? <button className="text-action" onClick={logout}><LogOut size={16} /><span>退出</span></button> : <button className="text-action" onClick={() => setLoginOpen(true)}><KeyRound size={16} /><span>登录</span></button>}
            <span className="operator-avatar">EA</span>
          </div>
        </header>

        {view === "overview" ? (
          <div className="view-stack">
            <section className="command-strip">
              <div>
                <span className="command-status"><i /> MULTI-AGENT RUNTIME</span>
                <h2>PR 风险治理工作台</h2>
                <p>从变更解析、并行审查到验证与版本门禁，运行状态集中在同一条链路。</p>
              </div>
              <div className="command-actions">
                <button className="button primary" onClick={() => navigate("review")}><Plus size={16} />新建审查</button>
                <button className="button secondary" onClick={() => navigate("tasks")}><History size={16} />任务记录</button>
              </div>
            </section>

            <section className="metrics-grid" aria-label="运行指标">
              {!dashboard && !dashboardError ? Array.from({ length: 6 }, (_, index) => <div className="metric-card skeleton" key={index} />) : metricItems.map(({ label, value, detail, icon: Icon, tone }) => (
                <article className={`metric-card tone-${tone}`} key={label}>
                  <span className="metric-icon"><Icon size={17} /></span>
                  <span className="metric-label">{label}</span>
                  <strong>{value}</strong>
                  <small>{detail}</small>
                </article>
              ))}
            </section>

            {dashboardError ? <div className="inline-error"><CircleAlert size={17} /><span><strong>无法读取运行数据</strong>{dashboardError}</span><button onClick={() => void loadDashboard()}>重试</button></div> : null}

            <section className="overview-grid">
              <div className="surface activity-surface">
                <div className="surface-header"><div><span className="kicker">RECENT ACTIVITY</span><h3>最近任务</h3></div><button className="inline-link" onClick={() => navigate("tasks")}>查看全部<ArrowRight size={14} /></button></div>
                {!dashboard ? <LoadingRows /> : <TaskTable compact tasks={(dashboard.tasks || []).slice(0, 5)} onOpen={(id) => void openTask(id)} />}
              </div>
              <div className="runtime-flow">
                <div className="surface-header"><div><span className="kicker">PIPELINE</span><h3>当前协作链</h3></div><span className="live-label"><i />在线</span></div>
                {[
                  { icon: ShieldCheck, label: "Security", detail: "注入、凭据与权限", mode: "并行" },
                  { icon: Activity, label: "Reliability", detail: "异常、日志与稳定性", mode: "并行" },
                  { icon: Bot, label: "Synthesis", detail: "证据过滤与最终门禁", mode: "汇总" },
                ].map(({ icon: Icon, label, detail, mode }, index) => (
                  <div className="flow-row" key={label}><span className="flow-index">0{index + 1}</span><Icon size={18} /><span><strong>{label}</strong><small>{detail}</small></span><em>{mode}</em></div>
                ))}
                <div className="runtime-foot"><Clock3 size={15} /><span>Checkpoint、预算与 Trace 由 Harness 统一管理</span></div>
              </div>
            </section>
          </div>
        ) : null}

        {view === "review" ? (
          <div className="view-stack">
            <SectionHeader eyebrow="NEW REVIEW" title="提交 Pull Request Diff" description="只分析统一 Diff 中新增的代码，并保留精确文件与行号。" />
            <section className="two-column review-layout">
              <form className="surface form-surface" onSubmit={submitReview}>
                <div className="form-grid">
                  <label>仓库地址<input name="repository" placeholder="owner/repository" autoComplete="off" required /></label>
                  <label>PR 编号 <span>可选</span><input name="pull_request" type="number" min="1" placeholder="42" /></label>
                </div>
                <label>Unified Diff<textarea name="diff" rows={17} spellCheck={false} required placeholder={"--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old_value\n+new_value"} /></label>
                <div className="form-actions">
                  <label className="checkbox"><input name="async" type="checkbox" defaultChecked /><span>放入异步队列</span></label>
                  <button className="button primary" disabled={busy === "review"} type="submit">{busy === "review" ? <LoaderCircle className="spin" size={16} /> : <Play size={16} />}开始审查</button>
                </div>
              </form>
              <aside className="review-context">
                <div className="context-section">
                  <span className="kicker">EXECUTION PLAN</span><h3>执行路径</h3>
                  {[
                    [SearchCode, "解析变更", "定位新增代码与文件行号"],
                    [Bot, "并行判断", "专职 Agent 独立输出证据"],
                    [ShieldCheck, "质量门禁", "过滤无定位和低可信结论"],
                    [Sparkles, "经验沉淀", "反馈进入失败案例库"],
                  ].map(([Icon, label, detail], index) => {
                    const StepIcon = Icon as LucideIcon;
                    return <div className="context-row" key={String(label)}><span>{index + 1}</span><StepIcon size={17} /><div><strong>{String(label)}</strong><small>{String(detail)}</small></div></div>;
                  })}
                </div>
                <div className="result-console"><div className="console-title"><span><i />RESULT</span><Code2 size={14} /></div><pre>{reviewResult ? formatJson(reviewResult) : "审查结果将在这里显示"}</pre></div>
              </aside>
            </section>
          </div>
        ) : null}

        {view === "tasks" ? (
          <div className="view-stack">
            <SectionHeader eyebrow="REVIEW HISTORY" title="审查任务" description="选择任务查看持久化状态、Trace 和最终报告。" />
            <section className="task-workspace">
              <div className="surface task-browser">
                <div className="surface-header"><div><span className="kicker">ALL TASKS</span><h3>任务列表</h3></div><span className="count-label">{tasks.length} 条记录</span></div>
                {tasksLoading ? <LoadingRows count={5} /> : <TaskTable tasks={tasks} onOpen={(id) => void openTask(id)} />}
              </div>
              <div className="detail-pane">
                <div className="surface-header"><div><span className="kicker">TASK DETAIL</span><h3>{selectedTask ? selectedTask.repository : "运行详情"}</h3></div>{selectedTask?.report && selectedTask.pull_request ? <button className="button danger" onClick={() => setFixDialogOpen(true)}><GitBranch size={15} />创建修复</button> : null}</div>
                {taskDetailLoading ? <LoadingRows count={6} /> : taskDetail ? <pre className="json-viewer">{formatJson(taskDetail)}</pre> : <EmptyState title="尚未选择任务" detail="从左侧列表打开一条记录" />}
              </div>
            </section>
          </div>
        ) : null}

        {view === "skills" ? (
          <div className="view-stack">
            <SectionHeader eyebrow="DYNAMIC CAPABILITIES" title="Skill 注册中心" description="检查能力版本、加载来源和隔离状态。" action={<button className="button secondary" onClick={() => void reloadSkills()} disabled={busy === "skills"}>{busy === "skills" ? <LoaderCircle className="spin" size={16} /> : <RotateCcw size={16} />}重新扫描</button>} />
            <section className="surface registry">
              <div className="registry-head"><span>Skill</span><span>能力描述</span><span>版本</span><span>来源</span><span>隔离</span></div>
              {skillsLoading ? <LoadingRows count={4} /> : skills.length ? skills.map((skill) => (
                <div className="registry-row" key={skill.name}>
                  <span className="skill-name"><span className="skill-icon"><Boxes size={16} /></span><strong>{skill.name}</strong></span>
                  <span className="skill-description">{skill.description || "暂无能力描述"}</span>
                  <code>v{skill.version}</code><span>{skill.source}</span>
                  <span className={skill.sandboxed ? "sandbox yes" : "sandbox"}><i />{skill.sandboxed ? "Sandboxed" : "Built-in"}</span>
                </div>
              )) : <EmptyState title="尚未加载 Skill" detail="重新扫描目录以加载可用能力" />}
            </section>
            {/* Added: preserve the existing registry while surfacing the new rule-evolution workflow. */}
            <section className="surface registry">
              <div className="surface-header">
                <div><span className="kicker">DECLARATIVE SKILL EVOLUTION</span><h3>Skill evolution</h3></div>
                <button className="button secondary" disabled={busy === "skill-evolution"} onClick={() => void autoEvolveSkill()}>
                  {busy === "skill-evolution" ? <LoaderCircle className="spin" size={16} /> : <Sparkles size={16} />}Auto propose
                </button>
              </div>
              <pre className="json-viewer">{skillEvolutionStatus ? formatJson(skillEvolutionStatus) : "No skill evolution status yet."}</pre>
              <div className="registry-head"><span>Skill</span><span>Candidate</span><span>Decision</span><span>Score</span><span>Action</span></div>
              {skillEvolutionRuns.length ? skillEvolutionRuns.map((run) => (
                <div className="registry-row" key={run.id}>
                  <span className="skill-name"><span className="skill-icon"><TestTube2 size={16} /></span><strong>{run.skill_name || "evolved-review"}</strong></span>
                  <code>v{run.candidate_version}</code>
                  <span>{run.decision}</span>
                  <code>{Number(run.candidate_score).toFixed(3)} / {Number(run.baseline_score).toFixed(3)}</code>
                  <button className="inline-link" disabled={busy === `skill-activate-${run.candidate_version}`} onClick={() => void activateSkillVersion(run.skill_name || "evolved-review", run.candidate_version)}>Activate</button>
                </div>
              )) : <EmptyState title="No evaluated skill candidates" detail="Create a review feedback case, then run auto propose." />}
              {skillEvolutionResult ? <pre className="json-viewer">{formatJson(skillEvolutionResult)}</pre> : null}
            </section>
          </div>
        ) : null}

        {view === "evolution" ? (
          <div className="view-stack">
            <SectionHeader eyebrow="REPLAY GATE" title="演进实验室" description="比较当前版本与候选 Prompt，只有通过非退化门禁才能激活。" action={<button className="button secondary" disabled={busy === "auto-evolve"} onClick={() => void autoEvolve()}>{busy === "auto-evolve" ? <LoaderCircle className="spin" size={16} /> : <Sparkles size={16} />}从反馈生成候选</button>} />
            <section className="evolution-status-bar">
              <div><span className="status-icon"><Activity size={17} /></span><span><small>EVALUATION STATUS</small><strong>{evolutionLoading ? "正在同步" : "评测状态已同步"}</strong></span></div>
              <pre>{evolutionStatus ? formatJson(evolutionStatus) : "暂无评测状态"}</pre>
            </section>
            <section className="two-column evolution-layout">
              <form className="surface form-surface" onSubmit={submitEvolution}>
                <span className="kicker">PROMPT CANDIDATE</span><h3>提交候选版本</h3>
                <label>Skill 名称<input name="skill_name" defaultValue="llm-review" required /></label>
                <label>候选提示词<textarea name="prompt" rows={12} required defaultValue="Review the unified diff. Return JSON findings with severity, fix and test. Report only actionable defects introduced by added lines." /></label>
                <div className="form-actions align-right"><button className="button primary" disabled={busy === "evolution"} type="submit">{busy === "evolution" ? <LoaderCircle className="spin" size={16} /> : <TestTube2 size={16} />}运行回放评测</button></div>
              </form>
              <div className="evolution-history">
                <div className="surface-header"><div><span className="kicker">LEARNING SIGNALS</span><h3>失败案例与最近版本</h3></div></div>
                {evolutionLoading ? <LoadingRows /> : (
                  <>
                    <div className="history-list">
                      {failures.slice(0, 6).map((item) => <div className="history-row" key={item.id}><CircleAlert size={16} /><span><strong>{item.category}</strong><small>{item.task_id}</small></span><span className={item.resolved ? "resolved" : "pending"}>{item.resolved ? "已解决" : "待处理"}</span></div>)}
                      {!failures.length ? <EmptyState title="暂无失败反馈" /> : null}
                    </div>
                    {runs.length ? <div className="run-history"><span className="kicker">RECENT EVALUATIONS</span>{runs.map((run) => <div className="run-row" key={run.id}><span>V{run.candidate_version}</span><strong>{run.decision}</strong><code>{Number(run.candidate_score).toFixed(3)} / {Number(run.baseline_score).toFixed(3)}</code></div>)}</div> : null}
                  </>
                )}
                {evolutionResult ? <div className="result-console compact"><div className="console-title"><span><i />LATEST RESULT</span></div><pre>{formatJson(evolutionResult)}</pre></div> : null}
              </div>
            </section>
          </div>
        ) : null}

        {view === "github" ? (
          <div className="view-stack">
            <SectionHeader eyebrow="REPOSITORY INTEGRATION" title="GitHub App" description="连接仓库事件、审查评论和独立修复分支。" />
            <section className="github-workspace">
              <div className="github-connect">
                <span className="github-mark"><GitPullRequest size={28} /></span>
                <div><span className="kicker">INSTALLATION</span><h2>接入现有研发工作流</h2><p>PR 创建或更新后自动进入异步审查队列，验证后的修复只写入独立分支。</p></div>
                <a className="button primary" href="/github/install">安装 GitHub App<ExternalLink size={15} /></a>
              </div>
              <div className="permission-table">
                <div className="surface-header"><div><span className="kicker">MINIMUM ACCESS</span><h3>权限范围</h3></div><ShieldCheck size={19} /></div>
                {[
                  ["Contents", "Read & write", "读取代码并创建修复提交"],
                  ["Pull requests", "Read & write", "读取 Diff 并 Upsert 评论"],
                  ["Metadata", "Read-only", "确认仓库与 Installation"],
                  ["Webhook", "Pull request", "接收 opened、reopened、synchronize"],
                ].map(([name, level, detail]) => <div className="permission-row" key={name}><span className="permission-code">{name.slice(0, 2).toUpperCase()}</span><span><strong>{name}</strong><small>{detail}</small></span><code>{level}</code></div>)}
              </div>
            </section>
          </div>
        ) : null}
      </main>

      {loginOpen ? (
        <div className="modal-backdrop" role="presentation">
          <form className="modal login-modal" onSubmit={submitLogin}>
            <div className="modal-heading"><span className="brand-symbol"><Code2 size={18} /></span><button type="button" onClick={() => setLoginOpen(false)} aria-label="关闭登录窗口"><X size={18} /></button></div>
            <span className="kicker">SECURE SESSION</span><h2>登录控制台</h2><p>使用绑定到工作区租户的凭据。</p>
            <label>用户名<input name="username" autoComplete="username" required autoFocus /></label>
            <label>密码<input name="password" type="password" autoComplete="current-password" required /></label>
            <label>租户 ID <span>可选</span><input name="tenant_id" placeholder="单租户可留空" /></label>
            <button className="button primary full" disabled={busy === "login"} type="submit">{busy === "login" ? <LoaderCircle className="spin" size={16} /> : <KeyRound size={16} />}登录</button>
            <div className="form-error" role="alert">{loginError}</div>
          </form>
        </div>
      ) : null}

      {fixDialogOpen ? (
        <div className="modal-backdrop" role="presentation">
          <div className="modal confirm-modal" role="dialog" aria-modal="true" aria-labelledby="fix-title">
            <div className="modal-heading"><span className="modal-icon"><GitBranch size={18} /></span><button type="button" onClick={() => setFixDialogOpen(false)} aria-label="关闭"><X size={18} /></button></div>
            <h2 id="fix-title">创建独立修复分支</h2><p>使用 PAT 时可留空；GitHub App 模式下填写 installation ID。</p>
            <label>Installation ID <span>可选</span><input value={installationId} onChange={(event) => setInstallationId(event.target.value)} inputMode="numeric" placeholder="例如 12345678" /></label>
            <div className="modal-actions"><button className="button secondary" onClick={() => setFixDialogOpen(false)}>取消</button><button className="button danger" disabled={busy === "fix"} onClick={() => void createFix()}>{busy === "fix" ? <LoaderCircle className="spin" size={16} /> : <GitBranch size={16} />}创建修复</button></div>
          </div>
        </div>
      ) : null}

      <div className={`toast ${toast ? "show" : ""}`} role="status"><CheckCircle2 size={16} />{toast}</div>
    </div>
  );
}
