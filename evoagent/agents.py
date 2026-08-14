"""多智能体审查：规划、协作质疑、取证、验证与仲裁。

协调器执行受预算约束的协作协议：
计划 -> 专项审查 -> 同行质疑 -> 证据修订 -> 独立验证 -> 仲裁。
接入任务存储时，每次交接都会持久化为 Agent 消息；专项审查器失败后会先重试，
再由规划器选择合适的替代审查器接管任务。
"""
import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

# 新增：为既有协作流程接入受限上下文、记忆和工具调用能力。
from .context_manager import ContextManager
from .diff_parser import ParsedDiff
from .memory import MemoryManager
from .models import Finding, Severity
from .reviewer import LocalRuleReviewer, Reviewer
from .runtime import AgentLoop, AgentTool, RuntimeBudgetExceeded, ToolRegistry


@dataclass
class AgentMessage:
    sender: str
    recipient: str
    kind: str
    content: Dict[str, Any]
    correlation_id: str = ""

    def to_dict(self) -> dict:
        # 将跨 Agent 传递的消息转换为可持久化、可返回给 API 的普通字典。
        return asdict(self)


class CollaborationBus:
    """按任务隔离的消息邮箱，并在有存储时保留可审计的协作记录。"""

    def __init__(self, task_id: str = "", store=None):
        # 初始化内存邮箱；只有带 task_id 和 store 时才会把消息同步写入数据库。
        self.task_id = task_id
        self.store = store
        self.messages: List[AgentMessage] = []
        self._lock = threading.Lock()

    def send(
        self, sender: str, recipient: str, kind: str,
        content: Dict[str, Any], correlation_id: str = "",
    ) -> AgentMessage:
        # 在锁内写入内存消息并同步持久化，避免并发专项审查器造成消息丢失或乱序。
        message = AgentMessage(sender, recipient, kind, content, correlation_id)
        with self._lock:
            self.messages.append(message)
            if self.store is not None and self.task_id:
                self.store.record_agent_message(self.task_id, message.to_dict())
        return message

    def inbox(self, recipient: str, correlation_id: str = "") -> List[dict]:
        # 按接收者和关联 id 查询可见消息；specialists/all 是协作协议中的广播地址。
        with self._lock:
            values = [
                message.to_dict() for message in self.messages
                if message.recipient in {recipient, "specialists", "all"}
                and (not correlation_id or message.correlation_id == correlation_id)
            ]
        return values

    def count(self, kind: str = "") -> int:
        # 统计消息类型，用于生成可观测的协作摘要，而不暴露消息具体内容。
        with self._lock:
            return sum(1 for item in self.messages if not kind or item.kind == kind)


@dataclass
class ReviewAssignment:
    agent: str
    objective: str
    files: List[str]
    risk_domains: List[str]
    assignment_id: str = ""
    round: int = 1
    reason: str = "initial-plan"

    def to_dict(self) -> dict:
        # 将分配单转换为消息和运行时上下文都能使用的稳定数据结构。
        return asdict(self)


@dataclass
class ReviewPlan:
    languages: List[str]
    changed_files: List[str]
    risk_level: str
    assignments: List[ReviewAssignment]

    def to_dict(self) -> dict:
        # 序列化审查计划，并递归转换其中的分配单，供审计和 Agent 消息使用。
        return {
            "languages": self.languages,
            "changed_files": self.changed_files,
            "risk_level": self.risk_level,
            "assignments": [item.to_dict() for item in self.assignments],
        }


@dataclass
class Critique:
    finding_key: str
    accepted: bool
    objections: List[str]
    confidence_adjustment: float
    questions: List[str]
    requires_revision: bool
    round: int = 1


@dataclass
class Reflection:
    finding_key: str
    revision_needed: bool
    guidance: List[str]
    round: int


@dataclass
class Reproduction:
    finding_key: str
    reproducible: bool
    method: str
    evidence: str


@dataclass
class VerificationDecision:
    finding_key: str
    approved: bool
    reasons: List[str]
    confidence: float


class CollaborationState(TypedDict, total=False):
    diff: str
    parsed: ParsedDiff
    task_id: str
    repository: str
    tenant_id: str
    bus: CollaborationBus
    plan: ReviewPlan
    specialist_findings: List[Finding]
    finding_sources: Dict[str, List[str]]
    assignments_by_agent: Dict[str, ReviewAssignment]
    critiques: Dict[str, Critique]
    reproductions: Dict[str, Reproduction]
    fix_ready: Dict[str, bool]
    decisions: Dict[str, VerificationDecision]
    verified: List[Finding]
    agent_outcomes: List[dict]
    rounds_completed: int
    collaboration_started: float
    collaboration_steps: int


def finding_key(finding: Finding) -> str:
    # 用“文件、变更行、规则”生成稳定短哈希，作为跨 Agent 质疑、证据和裁决的关联键。
    raw = "%s:%s:%s" % (finding.path, finding.line, finding.rule_id)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class FilteredAgent(Reviewer):
    # 把同一个 LocalRuleReviewer 包装成两个不同职责的 Agent。
    # 目前仅为第三方 Reviewer 兼容保留；内置 Agent 使用各自独立的规则集。
    """第三方审查器的兼容适配器；内置审查器不依赖这个包装层。"""

    def __init__(self, name: str, reviewer: Reviewer, prefixes: tuple):
        # 保存被包装审查器和规则前缀，同时从前缀推导其可声明的风险领域。
        self.name = name
        self.reviewer = reviewer
        self.prefixes = prefixes
        self.domains = tuple(
            "security" if item.startswith("SEC") else "reliability"
            for item in prefixes
        )

    def review(self, diff: str, parsed: ParsedDiff) -> List[Finding]:
        # 委托原审查器执行，再以规则前缀过滤结果，保证职责边界不重叠。
        return [
            item for item in self.reviewer.review(diff, parsed)
            if item.rule_id.startswith(self.prefixes)
        ]


class PlannerAgent:
    name = "planner-agent"

    DOMAIN_OBJECTIVES = {
        "security": "Trace attacker-controlled data and find exploitable security defects.",
        "reliability": "Find failure handling, observability and runtime reliability regressions.",
        "correctness": "Find behavior and data-flow defects introduced by the change.",
        "regression": "Identify compatibility and test gaps caused by the change.",
    }

    def plan(self, parsed: ParsedDiff, specialists: List[Reviewer]) -> ReviewPlan:
        # 在开始 review 之前分析 diff：识别语言、敏感文件和风险领域，再生成可追踪的分工计划。
        extensions = {path.rsplit(".", 1)[-1].lower() for path in parsed.files if "." in path}
        languages = sorted({
            "python" if ext == "py" else
            "javascript" if ext in {"js", "jsx", "ts", "tsx"} else
            "configuration" if ext in {"yml", "yaml", "json", "toml"} else ext
            for ext in extensions
        })
        sensitive = any(
            token in path.lower()
            for path in parsed.files
            for token in ("auth", "security", "payment", "permission", "token", "migration")
        )
        default_domains = ["security", "reliability", "correctness", "regression"]
        assignments = []
        for index, agent in enumerate(specialists, 1):
            declared = list(getattr(agent, "domains", ()) or default_domains)
            objectives = [
                self.DOMAIN_OBJECTIVES[item]
                for item in declared if item in self.DOMAIN_OBJECTIVES
            ]
            assignments.append(ReviewAssignment(
                agent=agent.name,
                objective=" ".join(objectives) or "Find actionable defects and cite changed-line evidence.",
                files=list(parsed.files),
                risk_domains=declared,
                assignment_id="A%02d" % index,
            ))
        return ReviewPlan(
            languages=languages or ["unknown"],
            changed_files=list(parsed.files),
            risk_level="high" if sensitive or len(parsed.files) > 10 else "normal",
            assignments=assignments,
        )

    def replan(
        self, failed: ReviewAssignment, substitutes: List[Reviewer], error: str,
    ) -> Optional[ReviewAssignment]:
        # 当原审查器反复失败时，从候选中选择风险领域重合最多的替代者，并记录接管原因。
        if not substitutes:
            return None
        target = max(
            substitutes,
            key=lambda item: len(
                set(getattr(item, "domains", ()) or failed.risk_domains)
                .intersection(failed.risk_domains)
            ),
        )
        return ReviewAssignment(
            agent=target.name,
            objective=(
                failed.objective
                + " Take over a failed assignment and independently reconstruct its evidence."
            ),
            files=list(failed.files), risk_domains=list(failed.risk_domains),
            assignment_id=failed.assignment_id, round=failed.round + 1,
            reason="replacement-after-failure: %s" % error[:160],
        )


class CriticAgent:
    name = "critic-agent"

    def challenge(
        self, finding: Finding, parsed: ParsedDiff,
        peer_sources: Optional[List[str]] = None, round_number: int = 1,
    ) -> Critique:
        # 审查报告质量，重点拦截没有新增行证据、不可执行修复建议和“幻觉式”高风险结论。
        objections = []
        questions = []
        valid_locations = {(line.path, line.line) for line in parsed.added_lines}
        if (finding.path, finding.line) not in valid_locations:
            objections.append("location is not an added line")
        source_line = next(
            (line.content for line in parsed.added_lines
             if line.path == finding.path and line.line == finding.line), ""
        )
        if not finding.evidence or finding.evidence.strip() not in source_line.strip():
            objections.append("quoted evidence does not match the changed line")
        if len(finding.explanation.strip()) < 12:
            objections.append("explanation is not specific enough")
        if len(finding.fix.strip()) < 8:
            objections.append("remediation is not actionable")
        if len(finding.test.strip()) < 8:
            objections.append("test strategy is not actionable")
        if len(peer_sources or []) < 2 and finding.severity in {Severity.CRITICAL, Severity.HIGH}:
            questions.append("High-impact claim needs independent verifier evidence.")
        adjustment = -.35 if objections else (.08 if len(peer_sources or []) > 1 else .03)
        return Critique(
            finding_key(finding), not objections, objections, adjustment,
            questions, bool(objections), round_number,
        )


class EvidenceAgent:
    name = "evidence-agent"

    def reproduce(self, finding: Finding, parsed: ParsedDiff) -> Reproduction:
        # 验证 Bug 真实性：独立回查变更行，给出可复现或无法复现的明确证据。
        line = next(
            (item.content for item in parsed.added_lines
             if item.path == finding.path and item.line == finding.line), ""
        )
        normalized = line.replace(" ", "")
        signatures = {
            "SEC-EVAL": ("eval(" in line or "exec(" in line),
            "SEC-SUBPROCESS-SHELL": "shell=True" in normalized,
            "SEC-HARDCODED-SECRET": any(
                token in line.lower() for token in ("password", "secret", "token", "api_key")
            ),
            "SEC-SQL-CONCAT": any(token in line for token in ("execute(", "query(")),
            "REL-DEBUG-PRINT": "print(" in line or "console.log(" in line,
            "REL-EMPTY-EXCEPT": "except" in line,
        }
        exact_evidence = bool(line and finding.evidence and finding.evidence.strip() in line.strip())
        reproducible = signatures.get(finding.rule_id, exact_evidence)
        return Reproduction(
            finding_key(finding), reproducible,
            "independent changed-line evidence check",
            line.strip()[:240] if reproducible else "No independently matching changed-line evidence.",
        )


class ReflectionAgent:
    name = "reflection-agent"

    def reflect(self, critique: Critique) -> Reflection:
        # 凭上游质疑和证据意见生成返工指引；原 Agent 仍须重新独立核验，不能直接相信消息内容。
        guidance = list(critique.objections)
        guidance.extend(critique.questions)
        if critique.requires_revision:
            guidance.append(
                "Re-read the changed line, discard unsupported assumptions, and return a corrected claim."
            )
        return Reflection(
            critique.finding_key, critique.requires_revision, guidance, critique.round
        )


class TestAgent(EvidenceAgent):
    """兼容仍导入 TestAgent 的外部集成；实际职责由 EvidenceAgent 实现。"""

    name = "test-agent"


class FixAgent:
    name = "fix-agent"

    def assess(self, finding: Finding) -> bool:
        # 拦截危险或空泛的修复建议，避免“关闭校验/吞掉异常”进入最终报告。
        dangerous = ("disable validation", "ignore error", "catch all")
        text = finding.fix.lower()
        return bool(finding.fix and not any(item in text for item in dangerous))


class VerifierAgent:
    name = "verifier-agent"

    def verify(
        self, finding: Finding, critique: Critique,
        reproduction: Reproduction, fix_ready: bool,
    ) -> VerificationDecision:
        # 汇总质疑、复现和修复建议三类门槛，给出带原因的独立放行或拒绝结论。
        reasons = []
        confidence = max(0.0, min(1.0, finding.confidence + critique.confidence_adjustment))
        if not critique.accepted:
            reasons.extend(critique.objections)
        if not reproduction.reproducible:
            reasons.append("independent evidence could not reproduce the claim")
        if not fix_ready:
            reasons.append("proposed remediation failed the safety/actionability gate")
        if confidence < .55:
            reasons.append("confidence is below the verification threshold")
        approved = not reasons
        return VerificationDecision(finding_key(finding), approved, reasons, confidence)


class ArbiterAgent:
    name = "arbiter-agent"

    def decide(
        self, findings: List[Finding], decisions: Dict[str, VerificationDecision],
    ) -> List[Finding]:
        # 仅放行通过独立验证的结论，并按位置和规则去重后生成最终报告清单。
        merged: Dict[tuple, Finding] = {}
        for finding in findings:
            decision = decisions[finding_key(finding)]
            if not decision.approved:
                continue
            finding.confidence = decision.confidence
            identity = (finding.path, finding.line, finding.rule_id)
            current = merged.get(identity)
            if current is None or finding.confidence > current.confidence:
                merged[identity] = finding
        order = {Severity.CRITICAL: 0, Severity.HIGH: 1, Severity.MEDIUM: 2, Severity.LOW: 3}
        return sorted(merged.values(), key=lambda item: (order[item.severity], item.path, item.line))


class SynthesizerAgent(ArbiterAgent):
    """兼容旧的 SynthesizerAgent 名称；现在最终合并职责由显式仲裁器承担。"""

    name = "synthesizer-agent"


class MultiAgentCoordinator(Reviewer):
    """具备协作对话、失败恢复和预算上限的多智能体审查协调器。"""

    name = "multi-agent-collaboration"

    def __init__(
        self, agents: List[Reviewer], max_workers: int = 4, store=None,
        agent_retries: int = 1, collaboration_rounds: int = 2,
        fallback_agent: Optional[Reviewer] = None,
        context_manager: Optional[ContextManager] = None,
        memory_manager: Optional[MemoryManager] = None,
        agent_loop_max_steps: int = 4, agent_loop_timeout_seconds: int = 45,
    ):
        # 注入专项审查器及全部运行边界；默认回退到本地规则审查器，保证单个 Agent 失败不让任务失效。
        self.agents = agents
        self.max_workers = max_workers
        self.store = store
        self.agent_retries = max(0, agent_retries)
        self.collaboration_rounds = max(1, collaboration_rounds)
        self.fallback_agent = fallback_agent or LocalRuleReviewer()
        self.context_manager = context_manager or ContextManager()
        self.memory_manager = memory_manager
        self.agent_loop = AgentLoop(agent_loop_max_steps, agent_loop_timeout_seconds)
        self.collaboration_max_steps = 8
        self.collaboration_timeout_seconds = 120
        self.planner = PlannerAgent()
        self.critic = CriticAgent()
        self.reflection_agent = ReflectionAgent()
        self.evidence_agent = EvidenceAgent()
        self.test_agent = self.evidence_agent
        self.fix_agent = FixAgent()
        self.verifier = VerifierAgent()
        self.arbiter = ArbiterAgent()
        self.synthesizer = self.arbiter
        self._summaries: Dict[str, dict] = {}
        self._summary_lock = threading.Lock()
        self.graph = self._build_graph()

    def _build_graph(self):
        # LangGraph 只表达协作阶段的拓扑；每个节点仍复用现有业务函数和消息留痕逻辑。
        builder = StateGraph(CollaborationState)
        builder.add_node("planner", self._guarded_node("planner", self._plan_node))
        builder.add_node("specialists", self._guarded_node("specialists", self._specialist_node))
        builder.add_node("deliberation", self._guarded_node("deliberation", self._deliberation_node))
        builder.add_node("evidence", self._guarded_node("evidence", self._evidence_node))
        builder.add_node("verifier", self._guarded_node("verifier", self._verify_node))
        builder.add_node("arbiter", self._guarded_node("arbiter", self._arbitrate_node))
        builder.add_edge(START, "planner")
        builder.add_edge("planner", "specialists")
        builder.add_edge("specialists", "deliberation")
        builder.add_edge("deliberation", "evidence")
        builder.add_edge("evidence", "verifier")
        builder.add_edge("verifier", "arbiter")
        builder.add_edge("arbiter", END)
        return builder.compile()

    def _guarded_node(self, name: str, handler):
        # 保留旧内层运行时的协作级预算：LangGraph 负责编排，包装器负责步数和时间边界。
        def run(state: CollaborationState) -> Dict[str, Any]:
            started = float(state.get("collaboration_started", time.monotonic()))
            steps = int(state.get("collaboration_steps", 0))
            if (
                steps >= self.collaboration_max_steps
                or time.monotonic() - started > self.collaboration_timeout_seconds
            ):
                raise RuntimeBudgetExceeded(
                    "multi-agent collaboration execution budget exceeded at %s" % name
                )
            output = handler(state) or {}
            if not isinstance(output, dict):
                raise TypeError("collaboration node %s must return a dict" % name)
            output["collaboration_started"] = started
            output["collaboration_steps"] = steps + 1
            return output

        return run

    def review(self, diff: str, parsed: ParsedDiff) -> List[Finding]:
        # 实现 Reviewer 标准接口；无任务上下文时复用带上下文入口并传入空任务 id。
        return self.review_with_context("", diff, parsed)

    def review_with_context(
        self, task_id: str, diff: str, parsed: ParsedDiff,
        repository: str = "", tenant_id: str = "default",
    ) -> List[Finding]:
        # 协调器入口：运行有上限的计划、审查、质疑、取证、验证和仲裁流程，并保留完整协作摘要。
        state: CollaborationState = {
            "task_id": task_id, "diff": diff, "parsed": parsed,
            "repository": repository, "tenant_id": tenant_id,
            "bus": CollaborationBus(task_id, self.store),
            "collaboration_started": time.monotonic(),
            "collaboration_steps": 0,
        }
        result = self.graph.invoke(state)
        summary = self._make_summary(result)
        if task_id:
            with self._summary_lock:
                self._summaries[task_id] = summary
        return result["verified"]

    def collaboration_summary(self, task_id: str) -> dict:
        # 返回任务级协作摘要的副本，防止调用方修改协调器内存中缓存的审计结果。
        with self._summary_lock:
            return dict(self._summaries.get(task_id, {}))

    @staticmethod
    def _bus(state: CollaborationState) -> CollaborationBus:
        # 从运行时状态中取得唯一消息总线，避免节点各自创建邮箱导致协作记录分裂。
        return state["bus"]

    def _emit(
        self, state: CollaborationState, sender: str, recipient: str,
        kind: str, content: Dict[str, Any], correlation_id: str = "",
    ) -> None:
        # 统一发送协作消息，确保所有节点使用同一任务邮箱和相同的关联 id 规则。
        self._bus(state).send(sender, recipient, kind, content, correlation_id)

    def _plan_node(self, state: CollaborationState) -> Dict[str, Any]:
        # 生成计划并将每份任务分配通知对应专项审查器，随后建立按 Agent 名称索引的分配表。
        plan = self.planner.plan(state["parsed"], self.agents)
        for assignment in plan.assignments:
            self._emit(
                state, self.planner.name, assignment.agent, "assignment",
                assignment.to_dict(), assignment.assignment_id,
            )
        return {
            "plan": plan,
            "assignments_by_agent": {item.agent: item for item in plan.assignments},
        }

    def _recall_memories(
        self, state: CollaborationState, assignment: ReviewAssignment,
    ) -> List[dict]:
        # 仅在仓库和记忆功能均可用时检索同租户、同仓库经验，并将检索行为本身写入审计消息。
        if not self.memory_manager or not state.get("repository"):
            return []
        query = " ".join([
            assignment.objective, " ".join(assignment.files),
            " ".join(assignment.risk_domains),
        ])
        memories = self.memory_manager.recall(
            state.get("tenant_id", "default"), state.get("repository", ""), query
        )
        if memories:
            self._emit(
                state, "memory-manager", assignment.agent, "memory_recalled",
                {
                    "count": len(memories),
                    "memory_ids": [item["id"] for item in memories],
                    "scopes": sorted({item["scope"] for item in memories}),
                }, assignment.assignment_id,
            )
        return memories

    def _agent_tools(
        self, state: CollaborationState, assignment: ReviewAssignment,
    ) -> ToolRegistry:
        # 为当前分配创建最小只读工具集；工具只能查看本次 diff 与同仓库记忆，不能访问宿主系统。
        def search_diff(query: str, limit: int = 20):
            # 在原始 diff 中做大小写无关的受限文本查询，返回的命中数上限为 50。
            value = str(query).strip().lower()
            if not value:
                raise ValueError("search_diff query is required")
            hits = []
            for index, line in enumerate(state["diff"].splitlines(), 1):
                if value in line.lower():
                    hits.append({"diff_line": index, "content": line[:500]})
                if len(hits) >= max(1, min(int(limit), 50)):
                    break
            return hits

        def changed_line(path: str, line: int):
            # 只读取新增行集合中的指定位置，防止模型借此读取未授权的工作区文件。
            match = next((
                item for item in state["parsed"].added_lines
                if item.path == str(path) and item.line == int(line)
            ), None)
            if match is None:
                return {"found": False, "path": path, "line": line}
            return {
                "found": True, "path": match.path, "line": match.line,
                "content": match.content,
            }

        def list_changed_files():
            # 返回本次 PR 的文件清单，为 Agent 定位审查范围提供只读证据。
            return list(state["parsed"].files)

        def recall_memory(query: str, limit: int = 5):
            # 按当前租户和仓库检索历史经验；没有启用记忆时显式返回空结果。
            if not self.memory_manager or not state.get("repository"):
                return []
            return self.memory_manager.recall(
                state.get("tenant_id", "default"), state["repository"], str(query),
                limit=max(1, min(int(limit), 10)),
            )

        # 工具注册
        return ToolRegistry([
            AgentTool(
                "search_diff",
                "Search the PR diff for an exact case-insensitive text fragment.",
                {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                    },
                    "required": ["query"], "additionalProperties": False,
                },
                search_diff,
            ),
            AgentTool(
                "changed_line",
                "Read one added line by new-file path and line number.",
                {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "line": {"type": "integer", "minimum": 1},
                    },
                    "required": ["path", "line"], "additionalProperties": False,
                },
                changed_line,
            ),
            AgentTool(
                "list_changed_files",
                "List files changed by this PR.",
                {
                    "type": "object", "properties": {},
                    "additionalProperties": False,
                },
                list_changed_files,
            ),
            AgentTool(
                "recall_memory",
                "Recall repository-scoped review experience relevant to a query.",
                {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                    },
                    "required": ["query"], "additionalProperties": False,
                },
                recall_memory,
            ),
        ])

    def _run_agent_loop(
        self, state: CollaborationState, agent: Reviewer,
        assignment: ReviewAssignment, feedback: Optional[List[str]],
    ) -> tuple:
        # 为支持工具调用的 Agent 构建上下文、受限工具和事件记录，并强制校验最终输出类型。
        memories = self._recall_memories(state, assignment)
        bundle = self.context_manager.build(state["diff"], assignment.to_dict(), memories)
        self._emit(
            state, "context-manager", agent.name, "context_prepared",
            bundle.metadata(), assignment.assignment_id,
        )
        tools = self._agent_tools(state, assignment)

        def on_event(kind: str, detail: Dict[str, Any]) -> None:
            # 将 Loop 事件写入协作总线，并把低重要性运行观察保存为可过期的工作记忆。
            self._emit(
                state, "agent-runtime", agent.name, kind, detail,
                assignment.assignment_id,
            )
            if self.memory_manager and state.get("task_id") and state.get("repository"):
                self.memory_manager.remember(
                    state.get("tenant_id", "default"), state["repository"],
                    "working", kind, str(detail), task_id=state["task_id"],
                    agent=agent.name, importance=0.3,
                )

        loop_state = {
            "diff": state["diff"], "context": bundle.text,
            "context_metadata": bundle.metadata(), "parsed": state["parsed"],
            "assignment": assignment.to_dict(), "feedback": list(feedback or []),
            "inbox": self._bus(state).inbox(agent.name, assignment.assignment_id),
            "memories": memories, "available_tools": tools.catalog(),
        }
        last_context = {"metadata": bundle.metadata()}

        def managed_step(loop_iteration: Dict[str, Any]) -> Dict[str, Any]:
            # 每一步重新压缩上下文并附加最新工具观察，避免模型跨轮使用过期或超预算的信息。
            managed = self.context_manager.compose(
                bundle, assignment.to_dict(), feedback=list(feedback or []),
                inbox=loop_iteration.get("inbox") or [], memories=memories,
                observations=loop_iteration.get("observations") or [],
                tools=tools.catalog(),
            )
            metadata = managed.metadata()
            last_context["metadata"] = metadata
            self._emit(
                state, "context-manager", agent.name, "context_window_prepared",
                metadata, assignment.assignment_id,
            )
            prepared = dict(loop_iteration)
            prepared["context"] = managed.text
            prepared["managed_context"] = managed.text
            prepared["context_metadata"] = metadata
            return getattr(agent, "agent_step")(prepared)

        result = self.agent_loop.run(
            managed_step, tools, loop_state, on_event,
        )
        findings = list(result.output or [])
        if not all(isinstance(item, Finding) for item in findings):
            raise TypeError("agent loop final output must contain Finding objects")
        return findings, {
            "loop_steps": result.steps, "loop_stop_reason": result.stop_reason,
            "context": last_context["metadata"], "memories_recalled": len(memories),
            "tools_available": len(tools.names()),
            "tool_calls": len(result.observations),
        }

    def _invoke_agent(
        self, state: CollaborationState, agent: Reviewer,
        assignment: ReviewAssignment, feedback: Optional[List[str]] = None,
    ) -> tuple:
        # 调用单个审查器并按配置重试；每次尝试都留痕，最终返回结果、次数、错误和运行元数据。
        last_error = None
        for attempt in range(1, self.agent_retries + 2):
            self._emit(
                state, "coordinator", agent.name, "attempt_started",
                {"attempt": attempt, "round": assignment.round}, assignment.assignment_id,
            )
            try:
                loop_stepper = getattr(agent, "agent_step", None)
                execution = {"loop_steps": 0, "loop_stop_reason": "one-shot"}
                if loop_stepper:
                    findings, execution = self._run_agent_loop(
                        state, agent, assignment, feedback
                    )
                else:
                    collaborative = getattr(agent, "review_assignment", None)
                    if collaborative:
                        findings = collaborative(
                            state["diff"], state["parsed"], assignment.to_dict(),
                            list(feedback or []),
                            self._bus(state).inbox(agent.name, assignment.assignment_id),
                        )
                    else:
                        findings = agent.review(state["diff"], state["parsed"])
                self._emit(
                    state, agent.name, self.critic.name, "specialist_evidence",
                    {
                        "attempt": attempt, "round": assignment.round,
                        "findings": [item.to_dict() for item in findings],
                        "execution": execution,
                    }, assignment.assignment_id,
                )
                return findings, attempt, "", execution
            except Exception as exc:
                last_error = str(exc)
                self._emit(
                    state, agent.name, self.planner.name, "agent_failure",
                    {"attempt": attempt, "error": last_error[:1000]},
                    assignment.assignment_id,
                )
                if attempt <= self.agent_retries:
                    self._emit(
                        state, self.planner.name, agent.name, "retry_request",
                        {"next_attempt": attempt + 1, "reason": last_error[:500]},
                        assignment.assignment_id,
                    )
        return (
            [], self.agent_retries + 1, last_error or "unknown agent failure",
            {"loop_steps": 0, "loop_stop_reason": "failed"},
        )

    def _replacement_candidates(self, failed_agent: Reviewer) -> List[Reviewer]:
        # 排除已失败的 Agent，并在候选中缺少时补入本地规则回退审查器。
        values = [item for item in self.agents if item is not failed_agent]
        if all(item.name != self.fallback_agent.name for item in values):
            values.append(self.fallback_agent)
        return values

    def _run_assignment(
        self, state: CollaborationState, assignment: ReviewAssignment,
        agent: Reviewer,
    ) -> dict:
        # 执行单个分配；超过重试次数仍失败时，按规划器给出的领域匹配策略交接给替代 Agent。
        findings, attempts, error, execution = self._invoke_agent(
            state, agent, assignment
        )
        result = {
            "agent": agent.name, "assignment_id": assignment.assignment_id,
            "attempts": attempts, "status": "completed" if not error else "failed",
            "findings": findings, "error": error, "substituted_for": "",
            "assignment": assignment, "execution": execution,
        }
        if not error:
            return result
        replacement = self.planner.replan(
            assignment, self._replacement_candidates(agent), error
        )
        if replacement is None:
            return result
        substitute = next(
            item for item in self._replacement_candidates(agent)
            if item.name == replacement.agent
        )
        self._emit(
            state, self.planner.name, substitute.name, "assignment_handoff",
            {
                "from": agent.name, "reason": error[:500],
                "assignment": replacement.to_dict(),
            }, assignment.assignment_id,
        )
        findings, replacement_attempts, replacement_error, replacement_execution = self._invoke_agent(
            state, substitute, replacement, ["Take over after %s failed: %s" % (agent.name, error)]
        )
        return {
            "agent": substitute.name, "assignment_id": assignment.assignment_id,
            "attempts": attempts + replacement_attempts,
            "status": "completed" if not replacement_error else "failed",
            "findings": findings, "error": replacement_error,
            "substituted_for": agent.name,
            "assignment": replacement, "execution": replacement_execution,
        }

    def _specialist_node(self, state: CollaborationState) -> Dict[str, Any]:
        # 并发执行所有专项分配，汇总发现与来源；所有分配失败才让整个审查任务失败。
        outcomes = []
        by_name = {item.name: item for item in self.agents}
        assignments = state["plan"].assignments
        with ThreadPoolExecutor(
            max_workers=min(self.max_workers, max(1, len(assignments)))
        ) as pool:
            futures = {
                pool.submit(self._run_assignment, state, assignment, by_name[assignment.agent]): assignment
                for assignment in assignments
            }
            for future in as_completed(futures):
                outcomes.append(future.result())
        findings = []
        sources: Dict[str, List[str]] = {}
        assignment_map = dict(state["assignments_by_agent"])
        for outcome in outcomes:
            assignment_map[outcome["agent"]] = outcome["assignment"]
            for finding in outcome["findings"]:
                key = finding_key(finding)
                sources.setdefault(key, []).append(outcome["agent"])
                findings.append(finding)
        if outcomes and all(item["status"] == "failed" for item in outcomes):
            raise RuntimeError(
                "all review assignments failed after retry/replanning: "
                + "; ".join(item["error"] for item in outcomes)
            )
        return {
            "specialist_findings": findings,
            "finding_sources": sources,
            "agent_outcomes": outcomes,
            "assignments_by_agent": assignment_map,
        }

    def _deliberation_node(self, state: CollaborationState) -> Dict[str, Any]:
        # 按上限轮次执行“质疑 -> 反思 -> 修订”，仅接受与原 finding 关联键一致的修订结果。
        findings = list(state["specialist_findings"])
        critiques: Dict[str, Critique] = {}
        rounds_completed = 0
        agents = {item.name: item for item in self.agents}
        agents.setdefault(self.fallback_agent.name, self.fallback_agent)
        assignments = state["assignments_by_agent"]
        for round_number in range(1, self.collaboration_rounds + 1):
            rounds_completed = round_number
            revisions = []
            for finding in findings:
                key = finding_key(finding)
                critique = self.critic.challenge(
                    finding, state["parsed"], state["finding_sources"].get(key, []),
                    round_number,
                )
                critiques[key] = critique
                reflection = self.reflection_agent.reflect(critique)
                self._emit(
                    state, self.critic.name, self.reflection_agent.name,
                    "critique_for_reflection", asdict(critique), key,
                )
                recipients = state["finding_sources"].get(key, []) or ["specialists"]
                for recipient in recipients:
                    self._emit(
                        state, self.critic.name, recipient, "peer_challenge",
                        asdict(critique), key,
                    )
                    self._emit(
                        state, self.reflection_agent.name, recipient,
                        "reflection_guidance", asdict(reflection), key,
                    )
                if reflection.revision_needed:
                    revisions.append((finding, critique, reflection, recipients[0]))
            if not revisions or round_number >= self.collaboration_rounds:
                break
            revised_by_key = {}
            for original, critique, reflection, source in revisions:
                assignment = assignments.get(source)
                agent = agents.get(source)
                if not assignment or not agent:
                    continue
                revised_assignment = ReviewAssignment(
                    agent=source, objective=assignment.objective,
                    files=list(assignment.files), risk_domains=list(assignment.risk_domains),
                    assignment_id=assignment.assignment_id, round=round_number + 1,
                    reason="critic-requested-revision",
                )
                self._emit(
                    state, self.critic.name, source, "revision_request",
                    {"objections": critique.objections, "guidance": reflection.guidance},
                    finding_key(original),
                )
                revised, _attempts, error, _execution = self._invoke_agent(
                    state, agent, revised_assignment,
                    reflection.guidance,
                )
                match = next(
                    (item for item in revised if finding_key(item) == finding_key(original)), None
                )
                if match is not None:
                    revised_by_key[finding_key(original)] = match
                    self._emit(
                        state, source, self.critic.name, "revision_response",
                        {"finding": match.to_dict(), "resolved": True}, finding_key(original),
                    )
                elif error:
                    self._emit(
                        state, source, self.critic.name, "revision_response",
                        {"resolved": False, "error": error[:500]}, finding_key(original),
                    )
            findings = [revised_by_key.get(finding_key(item), item) for item in findings]
        return {
            "specialist_findings": findings,
            "critiques": critiques,
            "rounds_completed": rounds_completed,
        }

    def _evidence_node(self, state: CollaborationState) -> Dict[str, Any]:
        # 对每条专项发现执行独立变更行取证，并把证据报告发送给验证器。
        reproductions = {}
        for finding in state["specialist_findings"]:
            reproduction = self.evidence_agent.reproduce(finding, state["parsed"])
            reproductions[reproduction.finding_key] = reproduction
            self._emit(
                state, self.evidence_agent.name, self.verifier.name, "evidence_report",
                asdict(reproduction), reproduction.finding_key,
            )
        return {"reproductions": reproductions}

    def _verify_node(self, state: CollaborationState) -> Dict[str, Any]:
        # 将修复建议安全性、质疑结论和复现证据汇合为可审计的逐条验证决策。
        fix_ready = {}
        decisions = {}
        for finding in state["specialist_findings"]:
            key = finding_key(finding)
            ready = self.fix_agent.assess(finding)
            fix_ready[key] = ready
            decision = self.verifier.verify(
                finding, state["critiques"][key], state["reproductions"][key], ready,
            )
            decisions[key] = decision
            self._emit(
                state, self.verifier.name, self.arbiter.name, "verification_decision",
                asdict(decision), key,
            )
        return {"fix_ready": fix_ready, "decisions": decisions}

    def _arbitrate_node(self, state: CollaborationState) -> Dict[str, Any]:
        # 仲裁已验证发现并写入最终消息；有记忆功能时归档结果，释放此任务的临时工作记忆。
        verified = self.arbiter.decide(
            state["specialist_findings"], state["decisions"]
        )
        rejected = [
            {"finding_key": key, "reasons": decision.reasons}
            for key, decision in state["decisions"].items() if not decision.approved
        ]
        self._emit(
            state, self.arbiter.name, "review-report", "arbitration_decision",
            {
                "approved_findings": [item.to_dict() for item in verified],
                "rejected_findings": rejected,
            },
        )
        if self.memory_manager and state.get("repository"):
            approved_keys = {finding_key(item) for item in verified}
            for finding in state["specialist_findings"]:
                key = finding_key(finding)
                decision = state["decisions"][key]
                self.memory_manager.remember_finding(
                    state.get("tenant_id", "default"), state["repository"],
                    state.get("task_id", ""), finding.to_dict(),
                    key in approved_keys, decision.reasons,
                )
            if state.get("task_id"):
                outcomes = state.get("agent_outcomes", [])
                memory_summary = {
                    "proposed_findings": len(state.get("specialist_findings", [])),
                    "approved_findings": len(verified),
                    "rejected_findings": len(rejected),
                    "dialogue_rounds": state.get("rounds_completed", 0),
                    "agent_loop_steps": sum(
                        int((item.get("execution") or {}).get("loop_steps", 0))
                        for item in outcomes
                    ),
                    "tool_calls": sum(
                        int((item.get("execution") or {}).get("tool_calls", 0))
                        for item in outcomes
                    ),
                }
                archived = self.memory_manager.consolidate_task(
                    state.get("tenant_id", "default"), state["repository"],
                    state["task_id"], memory_summary,
                )
                self._emit(
                    state, "memory-manager", "agent-runtime", "memory_consolidated",
                    {
                        "task_id": state["task_id"],
                        "summary_memory_id": (archived or {}).get("id", ""),
                        "working_memory_released": True,
                    },
                )
        return {"verified": verified}

    def _make_summary(self, state: CollaborationState) -> dict:
        # 将协作阶段、交接、工具调用和放行比例压缩成报告可展示的统计摘要。
        outcomes = state.get("agent_outcomes", [])
        decisions = state.get("decisions", {})
        return {
            "orchestrator": "langgraph",
            "protocol": "plan-challenge-revise-evidence-verify-arbitrate",
            "roles": [
                self.planner.name, "specialists", self.critic.name,
                self.reflection_agent.name,
                self.evidence_agent.name, self.verifier.name, self.arbiter.name,
            ],
            "planned_assignments": len(state.get("plan").assignments) if state.get("plan") else 0,
            "dialogue_rounds": state.get("rounds_completed", 0),
            "messages": self._bus(state).count(),
            "retries": self._bus(state).count("retry_request"),
            "handoffs": self._bus(state).count("assignment_handoff"),
            "agents": [
                {
                    "agent": item["agent"], "status": item["status"],
                    "attempts": item["attempts"],
                    "substituted_for": item.get("substituted_for", ""),
                    "loop_steps": (item.get("execution") or {}).get("loop_steps", 0),
                    "loop_stop_reason": (
                        item.get("execution") or {}
                    ).get("loop_stop_reason", "one-shot"),
                    "context_compressed": bool(
                        ((item.get("execution") or {}).get("context") or {}).get("compressed")
                    ),
                    "memories_recalled": (
                        item.get("execution") or {}
                    ).get("memories_recalled", 0),
                }
                for item in outcomes
            ],
            "agent_loop_steps": sum(
                int((item.get("execution") or {}).get("loop_steps", 0))
                for item in outcomes
            ),
            "context_compressions": sum(
                bool(((item.get("execution") or {}).get("context") or {}).get("compressed"))
                for item in outcomes
            ),
            "memories_recalled": sum(
                int((item.get("execution") or {}).get("memories_recalled", 0))
                for item in outcomes
            ),
            "proposed_findings": len(state.get("specialist_findings", [])),
            "approved_findings": sum(1 for item in decisions.values() if item.approved),
            "rejected_findings": sum(1 for item in decisions.values() if not item.approved),
        }
