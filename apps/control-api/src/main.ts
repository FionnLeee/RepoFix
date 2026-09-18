import "reflect-metadata";
import { randomBytes } from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import {
  Body,
  BadRequestException,
  ConflictException,
  Controller,
  Get,
  Headers,
  Injectable,
  Module,
  NotFoundException,
  OnModuleDestroy,
  OnModuleInit,
  Param,
  Post,
  Query,
  Req,
  Res,
  UnauthorizedException,
  ValidationPipe,
} from "@nestjs/common";
import { NestFactory } from "@nestjs/core";
import {
  FastifyAdapter,
  NestFastifyApplication,
} from "@nestjs/platform-fastify";
import { Prisma, PrismaClient } from "@prisma/client";
import { ChannelModel, ConfirmChannel, connect } from "amqplib";
import {
  IsIn,
  IsInt,
  IsObject,
  IsOptional,
  IsString,
  Max,
  MaxLength,
  Min,
  MinLength,
  Matches,
  IsArray,
  IsBoolean,
  ArrayMinSize,
  ArrayMaxSize,
  ArrayUnique,
  ValidateNested,
} from "class-validator";
import { Type } from "class-transformer";
import { readFileSync, existsSync } from "node:fs";
import { timingSafeEqual } from "node:crypto";
import { ContextService, ContextOwner, IndexBegin, IndexPublish, MemoryWrite, projectKey } from "./context";

const TASK =
  "修复 pricing.py 中 discounted_total：百分比折扣应按百分比计算，结果保留两位小数，并拒绝小于 0 或大于 100 的折扣。运行开发测试后提交补丁。";
const terminal = ["SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"];
const MAX_RECOVERY_ATTEMPTS = 3;
const resumablePhases = ["ready", "awaiting_approval"];
const pathPattern = /^(?!\/)(?!.*(?:^|\/)\.\.(?:\/|$))(?!.*(?:^|\/)\.(?:\/|$))(?!.*(?:^|\/)\.git(?:\/|$))[a-zA-Z0-9_.-]+(?:\/[a-zA-Z0-9_.-]+)*$/;
/** W3C trace context for one delivery: the worker continues the trace this control plane starts. */
const traceparent = () => `00-${randomBytes(16).toString("hex")}-${randomBytes(8).toString("hex")}-01`;
class RepositorySpec {
  @Matches(/^(registered:[a-zA-Z0-9_-]{1,60}|https:\/\/github\.com\/[a-zA-Z0-9_-]+\/[a-zA-Z0-9_.-]+)$/)
  @MaxLength(200) source!: string;
  @Matches(/^[0-9a-f]{40}$/) commit!: string;
  @IsOptional() @IsString() @MaxLength(200) subdir?: string;
  @IsArray() @ArrayMinSize(1) @ArrayMaxSize(30) @ArrayUnique()
  @IsString({ each: true }) @MaxLength(240, { each: true }) @Matches(pathPattern, { each: true })
  allowedPaths!: string[];
  @IsOptional() @IsString() @MaxLength(120) instanceId?: string;
  @IsOptional() @IsIn(["tests", "harness"]) verificationMode?: string;
  @IsOptional() @IsObject() verificationFiles?: Record<string, string>;
  @IsOptional() @IsString() @MinLength(1) @MaxLength(1000) testCommand?: string;
}
function catalog(): any[] {
  const path = process.env.BASELINE_CATALOG || "runtime/baseline-catalog.json";
  return existsSync(path) ? JSON.parse(readFileSync(path, "utf8")) : [];
}
function canonical(value: any): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value && typeof value === "object") return `{${Object.keys(value).sort().map(k => `${JSON.stringify(k)}:${canonical(value[k])}`).join(",")}}`;
  return JSON.stringify(value);
}
class CreateRun {
  @IsString() @MinLength(8) @MaxLength(100) requestKey!: string;
  @IsIn(["demo", "live"]) mode!: string;
  @IsOptional() @IsString() @MinLength(10) @MaxLength(10000) task?: string;
  @IsOptional() @ValidateNested() @Type(() => RepositorySpec) spec?: RepositorySpec;
  @IsOptional() @IsString() @MaxLength(60) baselineId?: string;
  @IsOptional() @IsIn(["full", "compact", "managed"]) contextMode?: string;
  @IsOptional() @IsBoolean() memoryEnabled?: boolean;
  @IsOptional() @IsIn(["auto", "strict"]) approvalPolicy?: string;
  @IsOptional() @IsIn(["auto", "off"]) reviewPolicy?: string;
  @IsOptional() @IsInt() @Min(0) @Max(5) reviewRounds?: number;
}
class Claim {
  @IsString() @MinLength(1) @MaxLength(100) workerId!: string;
}
class Step {
  @IsString() @MaxLength(100) workerId!: string;
  @IsInt() @Min(1) generation!: number;
  @IsString() @MaxLength(120) key!: string;
  @IsString() @MaxLength(50) type!: string;
  @IsObject() data!: Record<string, unknown>;
  @IsOptional()
  @IsIn(["VERIFYING", "SUCCEEDED", "FAILED", "CANCELLED", "WAITING_APPROVAL"])
  status?: string;
}
class Ownership {
  @IsString() @MaxLength(100) workerId!: string;
  @IsInt() @Min(1) generation!: number;
}
class ApprovalRequest extends Ownership {
  @IsObject() checkpoint!: { id: string; sequence: number };
  @Matches(/^[0-9a-f]{64}$/) workspaceSha256!: string;
  @IsObject() action!: Record<string, unknown>;
  @Matches(/^[0-9a-f]{64}$/) actionSha256!: string;
  @IsString() @MaxLength(2000) reason!: string;
  @IsArray() @ArrayMaxSize(20) targets!: unknown[];
  @IsIn(["auto", "strict"]) policy!: string;
}
class Decision {
  @IsIn(["approve", "reject"]) decision!: string;
  @IsOptional() @IsString() @MaxLength(1000) note?: string;
}

@Injectable()
class Store extends PrismaClient implements OnModuleInit, OnModuleDestroy {
  connection?: ChannelModel;
  channel?: ConfirmChannel;
  timer?: NodeJS.Timeout;
  busy = false;
  async onModuleInit() {
    await this.$connect();
    await this.ensureChannel();
    this.timer = setInterval(() => {
      void this.tick().catch((e) => console.error("coordinator:", e.message));
    }, 1000);
  }
  async onModuleDestroy() {
    clearInterval(this.timer);
    try {
      await this.channel?.close();
      await this.connection?.close();
    } catch {
      // shutting down with an already broken broker connection is not an error state
    }
    await this.$disconnect();
  }
  async ensureChannel(): Promise<ConfirmChannel> {
    if (this.channel) return this.channel;
    const connection = await connect(process.env.AMQP_URL!);
    connection.on("error", (error) => console.error("broker:", error.message));
    connection.on("close", () => {
      if (this.connection === connection) {
        this.channel = undefined;
        this.connection = undefined;
      }
    });
    const channel = await connection.createConfirmChannel();
    await channel.assertQueue("repopilot.runs.v1", { durable: true });
    this.connection = connection;
    this.channel = channel;
    return channel;
  }
  async resetChannel() {
    const connection = this.connection;
    this.channel = undefined;
    this.connection = undefined;
    try {
      await connection?.close();
    } catch {
      // the connection is being discarded either way
    }
  }
  /** Latest control-plane-registered checkpoint the next attempt (``nextGeneration``) may resume from. */
  async resumable(runId: string, nextGeneration: number) {
    const events = await this.event.findMany({
      where: { runId, type: "CHECKPOINT_SAVED" },
      orderBy: { id: "asc" },
    });
    const candidates = events
      .map((event) => event.data as any)
      .filter((data) => data && Number.isInteger(data.generation) && data.generation < nextGeneration
        && resumablePhases.includes(data.phase));
    return candidates.length ? candidates[candidates.length - 1] : null;
  }
  async tick() {
    if (this.busy) return;
    this.busy = true;
    try {
      const channel = await this.ensureChannel().catch((error) => {
        console.error("broker:", error.message);
        return null;
      });
      if (channel)
        for (const item of await this.outbox.findMany({ where: { publishedAt: null }, orderBy: { id: "asc" }, take: 20 })) {
          try {
            channel.sendToQueue(
              "repopilot.runs.v1",
              Buffer.from(
                JSON.stringify({
                  schema_version: 1,
                  message_id: String(item.id),
                  run_id: item.runId,
                  traceparent: traceparent(),
                }),
              ),
              { persistent: true },
            );
            await channel.waitForConfirms();
          } catch (error) {
            // Keep the outbox row; a later tick republishes after reconnecting.
            console.error("publish:", (error as Error).message);
            await this.resetChannel();
            break;
          }
          await this.outbox.update({
            where: { id: item.id },
            data: { publishedAt: new Date() },
          });
        }
      const expired = await this.run.findMany({
        where: {
          status: { in: ["RUNNING", "VERIFYING"] },
          leaseUntil: { lt: new Date() },
        },
      });
      for (const run of expired) {
        // The next attempt will run as the following generation; its own checkpoints are the
        // candidates it may resume from.
        const resume = await this.resumable(run.id, run.generation + 1);
        const cancelled = run.cancelRequested;
        const recover = !cancelled && !!resume && run.recoveryAttempts < MAX_RECOVERY_ATTEMPTS;
        await this.$transaction(async (tx) => {
          const changed = await tx.run.updateMany({
            where: {
              id: run.id,
              generation: run.generation,
              status: { in: ["RUNNING", "VERIFYING"] },
              leaseUntil: { lt: new Date() },
            },
            data: recover
              ? { status: "QUEUED", workerId: null, leaseUntil: null, recoveryAttempts: { increment: 1 } }
              : { status: cancelled ? "CANCELLED" : "INTERRUPTED", workerId: null, leaseUntil: null },
          });
          if (!changed.count) return;
          if (recover) {
            await tx.event.create({
              data: {
                runId: run.id,
                key: `requeued-${run.generation}`,
                type: "REQUEUED",
                data: {
                  reason: "Worker 租约过期；按已登记的最新检查点重新入队，由新的执行代次恢复。",
                  attempt: run.recoveryAttempts + 1,
                  checkpoint_id: resume!.id,
                  from_generation: resume!.generation,
                  sequence: resume!.sequence,
                  phase: resume!.phase,
                },
              },
            });
            await tx.outbox.create({ data: { runId: run.id } });
          } else if (cancelled) {
            // The attempt is gone, so no worker will report the cancellation it was asked for.
            await tx.event.create({
              data: {
                runId: run.id,
                key: `cancelled-${run.generation}`,
                type: "CANCELLED",
                data: { reason: "任务已请求取消；持有该次尝试的 Worker 失联，按取消结束。" },
              },
            });
          } else {
            await tx.event.create({
              data: {
                runId: run.id,
                key: `lease-expired-${run.generation}`,
                type: "INTERRUPTED",
                data: {
                  reason: resume
                    ? `Worker 租约过期；自动恢复已用满 ${run.recoveryAttempts} 次，停在中断状态。`
                    : "Worker 租约过期；没有已登记的可恢复检查点，停在中断状态。",
                },
              },
            });
          }
        });
      }
    } finally {
      this.busy = false;
    }
  }
}

@Controller()
class Api {
  constructor(private readonly db: Store) {}
  authorize(value?: string) {
    const expected = `Bearer ${process.env.WORKER_TOKEN}`;
    if (
      !process.env.WORKER_TOKEN ||
      !value ||
      value.length !== expected.length ||
      !timingSafeEqual(Buffer.from(value), Buffer.from(expected))
    )
      throw new UnauthorizedException();
  }
  @Get("health") async health() {
    await this.db.$queryRaw`SELECT 1`;
    return {
      status: "ok",
      upstream: "mini-swe-agent 2.4.6",
      milestone: "M4",
      liveEnabled: process.env.LIVE_ENABLED === "true",
    };
  }
  @Get("runs") list() {
    return this.db.run.findMany({ orderBy: { createdAt: "desc" }, take: 40 });
  }
  @Get("baseline-tasks") baselines() {
    return catalog().map(({ id, title, task, spec }) => ({ id, title, task, spec }));
  }
  @Get("projects") projects() {
    return this.db.project.findMany({ where: { owner: "local" }, orderBy: { createdAt: "desc" } });
  }
  @Get("projects/:id/memories") memories(@Param("id") id: string) {
    return new ContextService(this.db).memories(id);
  }
  @Post("projects/:id/memories") memoryCreate(@Param("id") id: string, @Body() body: MemoryWrite) {
    return new ContextService(this.db).writeMemory(id, body);
  }
  @Post("projects/:id/memories/:memoryId") memoryUpdate(@Param("id") id: string,
    @Param("memoryId") memoryId: string, @Body() body: MemoryWrite) {
    return new ContextService(this.db).writeMemory(id, body, memoryId);
  }
  @Get("projects/:id/indexes") indexes(@Param("id") id: string) {
    return this.db.indexHead.findMany({ where: { projectId: id }, orderBy: { updatedAt: "desc" } });
  }
  @Post("runs/:id/compact") async compact(@Param("id") id: string) {
    const changed = await this.db.run.updateMany({ where: { id, contextMode: "managed", status: { in: ["QUEUED", "RUNNING"] } },
      data: { compactRequested: { increment: 1 } } });
    if (!changed.count) throw new ConflictException("仅进行中的 managed 任务支持压缩请求");
    return { requested: true };
  }
  @Post("internal/runs/:id/context") context(@Param("id") id: string,
    @Headers("authorization") token: string, @Body() body: ContextOwner) {
    this.authorize(token);
    return new ContextService(this.db).state(id, body);
  }
  @Post("internal/runs/:id/indexes/begin") indexBegin(@Param("id") id: string,
    @Headers("authorization") token: string, @Body() body: IndexBegin) {
    this.authorize(token);
    return new ContextService(this.db).begin(id, body);
  }
  @Post("internal/runs/:id/indexes/publish") indexPublish(@Param("id") id: string,
    @Headers("authorization") token: string, @Body() body: IndexPublish) {
    this.authorize(token);
    return new ContextService(this.db).publish(id, body);
  }
  @Post("runs") async create(@Body() body: CreateRun) {
    if (body.mode === "live" && process.env.LIVE_ENABLED !== "true")
      throw new ConflictException("真实模型尚未配置");
    if (body.baselineId && (body.spec || body.task))
      throw new BadRequestException("基线任务不能同时覆盖仓库或任务说明");
    const baseline = body.baselineId ? catalog().find(t => t.id === body.baselineId) : null;
    if (body.baselineId && !baseline) throw new BadRequestException("基线任务不存在，请先初始化任务集");
    const rawSpec = baseline?.spec || body.spec;
    const spec = rawSpec ? { ...rawSpec, subdir: rawSpec.subdir || "", testCommand: rawSpec.testCommand || "python -m unittest discover -v" } : null;
    const task = baseline?.task || body.task || TASK;
    const contextMode = body.contextMode || (spec ? "managed" : "full");
    const memoryEnabled = !!spec && contextMode === "managed" && (body.memoryEnabled ?? !body.baselineId);
    const approvalPolicy = body.approvalPolicy || "auto";
    // The reviewer needs a candidate patch to read, so a single-file task keeps review off.
    const reviewPolicy = spec ? body.reviewPolicy || "auto" : "off";
    // The bounded revision count is the reviewer's budget, not the coder's; NULL means default.
    const reviewRounds = spec ? body.reviewRounds ?? null : null;
    const projectId = spec ? projectKey(spec.source, spec.subdir) : null;
    if (spec) {
      if (!baseline && !body.task) throw new BadRequestException("指定仓库需要任务说明");
      if (body.mode === "demo" && !baseline) throw new BadRequestException("自定义仓库仅支持真实模型；预设演示仅用于内置基线");
      if ((spec.subdir && !pathPattern.test(spec.subdir)) || spec.allowedPaths.some((p: string) => p.toLowerCase().split("/").includes(".git") || p.startsWith("_repopilot_verify/")))
        throw new BadRequestException("仓库路径无效");
      const entries = Object.entries(spec.verificationFiles || {});
      if (spec.verificationMode === "harness") {
        // The official harness applies the instance's own test patch, so this run carries none.
        if (entries.length) throw new BadRequestException("交由官方 harness 验收的任务不应携带验收测试");
      } else if (!entries.length || entries.length > 20 || !entries.some(([p]) => /^test_[a-zA-Z0-9_]+\.py$/.test(p)) ||
          entries.some(([p, v]) => !pathPattern.test(p) || p.toLowerCase().split("/").includes(".git") || typeof v !== "string" || Buffer.byteLength(v) > 100000 || v.includes("\0")))
        throw new BadRequestException("验收文件必须包含 test_*.py，使用相对路径且单文件不超过 100 KB");
    } else if (body.task || contextMode !== "full") {
      throw new BadRequestException("任务说明与上下文模式需要指定仓库或基线任务");
    }
    const matches = (run: any) => run.mode === body.mode && run.task === task &&
      canonical(run.spec) === canonical(spec) && run.baselineId === (body.baselineId || null) &&
      run.contextMode === contextMode && run.memoryEnabled === memoryEnabled &&
      run.approvalPolicy === approvalPolicy && run.reviewPolicy === reviewPolicy &&
      (run.reviewRounds ?? null) === reviewRounds;
    const existing = await this.db.run.findUnique({
      where: { requestKey: body.requestKey },
    });
    if (existing) {
      if (!matches(existing))
        throw new ConflictException("幂等键对应不同的任务配置");
      return existing;
    }
    try {
      return await this.db.$transaction(async (tx) => {
        if (projectId) await tx.project.upsert({ where: { id: projectId }, update: {},
          create: { id: projectId, source: spec.source, subdir: spec.subdir } });
        const run = await tx.run.create({
          data: { requestKey: body.requestKey, mode: body.mode, task,
            ...(spec ? { spec: spec as Prisma.InputJsonValue } : {}), baselineId: body.baselineId,
            contextMode, projectId, memoryEnabled, approvalPolicy, reviewPolicy, reviewRounds },
        });
        await tx.event.create({
          data: {
            runId: run.id,
            key: "created",
            type: "QUEUED",
            data: { mode: run.mode, fixture: body.baselineId || (spec ? "repository" : "pricing-percent-v1"), contextMode },
          },
        });
        await tx.outbox.create({ data: { runId: run.id } });
        return run;
      });
    } catch (e) {
      if (
        e instanceof Prisma.PrismaClientKnownRequestError &&
        e.code === "P2002"
      ) {
        const run = await this.db.run.findUniqueOrThrow({
          where: { requestKey: body.requestKey },
        });
        if (!matches(run))
          throw new ConflictException("幂等键对应不同的任务配置");
        return run;
      }
      throw e;
    }
  }
  @Get("runs/:id") async detail(@Param("id") id: string) {
    const run = await this.db.run.findUnique({
      where: { id },
      include: { events: { orderBy: { id: "asc" } }, approvals: { orderBy: { requestedAt: "desc" } } },
    });
    if (!run) throw new NotFoundException();
    return run;
  }
  @Get("runs/:id/checkpoints") async checkpoints(@Param("id") id: string) {
    if (!(await this.db.run.findUnique({ where: { id } }))) throw new NotFoundException();
    return this.db.event.findMany({
      where: { runId: id, type: "CHECKPOINT_SAVED" }, orderBy: { id: "asc" },
    });
  }
  @Get("runs/:id/approvals") async approvals(@Param("id") id: string) {
    if (!(await this.db.run.findUnique({ where: { id } }))) throw new NotFoundException();
    return this.db.approval.findMany({ where: { runId: id }, orderBy: { requestedAt: "desc" } });
  }
  @Post("runs/:id/approvals/:approvalId/decide") async decide(
    @Param("id") id: string,
    @Param("approvalId") approvalId: string,
    @Body() body: Decision,
  ) {
    const run = await this.db.run.findUnique({ where: { id } });
    if (!run) throw new NotFoundException();
    const approval = await this.db.approval.findFirst({ where: { id: approvalId, runId: id } });
    if (!approval) throw new NotFoundException();
    if (terminal.includes(run.status) || run.cancelRequested) {
      await this.db.approval.updateMany({
        where: { id: approvalId, runId: id, status: "PENDING" },
        data: { status: "INVALIDATED", decidedAt: new Date() },
      });
      throw new ConflictException("任务已经结束，审批无法再决定");
    }
    return this.db.$transaction(async (tx) => {
      await tx.$queryRaw`SELECT id FROM "Run" WHERE id = ${id} FOR UPDATE`;
      const decision = body.decision === "approve" ? "APPROVED" : "REJECTED";
      const current = await tx.approval.findUniqueOrThrow({ where: { id: approvalId } });
      if (current.status !== "PENDING") {
        if (current.status === decision) return { approval: current, run, repeated: true };
        throw new ConflictException("审批已经决定，不能改判");
      }
      const live = await tx.run.findUniqueOrThrow({ where: { id } });
      if (live.status !== "WAITING_APPROVAL")
        throw new ConflictException("任务当前不在等待审批状态");
      const approval = await tx.approval.update({
        where: { id: approvalId },
        data: { status: decision, decidedAt: new Date(), note: body.note ?? null },
      });
      await tx.event.create({
        data: {
          runId: id,
          key: `approval-decided-${approval.id}`,
          type: "APPROVAL_DECIDED",
          data: {
            approval_id: approval.id, decision, action_sha256: approval.actionSha256,
            workspace_sha256: approval.workspaceSha256, note: body.note ?? null,
          },
        },
      });
      // The decision only re-queues; the resumed attempt re-validates the binding and runs the action.
      const next = await tx.run.update({
        where: { id },
        data: { status: "QUEUED", workerId: null, leaseUntil: null },
      });
      await tx.outbox.create({ data: { runId: id } });
      return { approval, run: next, repeated: false };
    });
  }
  @Get("internal/runs/active") async active(@Headers("authorization") token: string) {
    this.authorize(token);
    const runs = await this.db.run.findMany({
      where: { status: { in: ["RUNNING", "VERIFYING"] }, leaseUntil: { gt: new Date() } },
      select: { id: true },
    });
    return { runs: runs.map((run) => run.id) };
  }
  @Post("internal/runs/:id/resume-plan") async resumePlan(
    @Param("id") id: string,
    @Headers("authorization") token: string,
    @Body() body: Ownership,
  ) {
    this.authorize(token);
    const run = await this.db.run.findUnique({ where: { id } });
    if (!run) throw new NotFoundException();
    if (run.workerId !== body.workerId || run.generation !== body.generation ||
        !run.leaseUntil || run.leaseUntil < new Date())
      throw new ConflictException("运行所有权失效");
    const checkpoint = await this.db.resumable(id, run.generation);
    if (!checkpoint) return { mode: "fresh" };
    if (checkpoint.phase !== "awaiting_approval") return { mode: "restore", checkpoint, approval: null };
    const approval = await this.db.approval.findFirst({
      where: { runId: id, checkpointId: checkpoint.id }, orderBy: { requestedAt: "desc" },
    });
    if (!approval) return { mode: "wait", checkpoint, approval: null };
    return approval.status === "PENDING"
      ? { mode: "wait", checkpoint, approval }
      : { mode: "restore", checkpoint, approval };
  }
  @Post("internal/runs/:id/approvals") async requestApproval(
    @Param("id") id: string,
    @Headers("authorization") token: string,
    @Body() body: ApprovalRequest,
  ) {
    this.authorize(token);
    return this.db.$transaction(async (tx) => {
      await tx.$queryRaw`SELECT id FROM "Run" WHERE id = ${id} FOR UPDATE`;
      const run = await tx.run.findUnique({ where: { id } });
      if (!run) throw new NotFoundException();
      const existing = await tx.approval.findUnique({
        where: { runId_checkpointId_actionSha256: {
          runId: id, checkpointId: body.checkpoint.id, actionSha256: body.actionSha256 } },
      });
      if (existing) {
        // A retried request for the same action and checkpoint must not create a second row.
        if (existing.status !== "PENDING") throw new ConflictException("该动作的审批已经决定");
        return existing;
      }
      if (terminal.includes(run.status) || run.cancelRequested) throw new ConflictException("任务已经结束");
      if (run.workerId !== body.workerId || run.generation !== body.generation ||
          !run.leaseUntil || run.leaseUntil < new Date())
        throw new ConflictException("运行所有权失效");
      if (run.status !== "RUNNING") throw new ConflictException("只有执行中的任务可以提交审批");
      if (!/^[0-9a-f]{32}$/.test(String(body.checkpoint.id)) || !Number.isInteger(body.checkpoint.sequence))
        throw new BadRequestException("检查点元数据无效");
      const checkpoint = (await tx.event.findMany({ where: { runId: id, type: "CHECKPOINT_SAVED" } }))
        .map((event) => event.data as any)
        .find((data) => data?.id === body.checkpoint.id);
      // A resumed attempt re-requests approval for a checkpoint recorded by an earlier attempt,
      // so the checkpoint's own generation may be older than this attempt's.
      if (!checkpoint || checkpoint.generation > body.generation ||
          checkpoint.sequence !== body.checkpoint.sequence || checkpoint.phase !== "awaiting_approval" ||
          checkpoint.workspace_sha256 !== body.workspaceSha256 || checkpoint.action_sha256 !== body.actionSha256)
        throw new BadRequestException("审批必须绑定已登记且与动作一致的检查点");
      const pending = await tx.approval.findFirst({ where: { runId: id, status: "PENDING" } });
      if (pending) throw new ConflictException("已有待决定的审批");
      const approval = await tx.approval.create({
        data: {
          runId: id, generation: body.generation, action: body.action as Prisma.InputJsonValue,
          actionSha256: body.actionSha256, reason: body.reason, targets: body.targets as Prisma.InputJsonValue,
          policy: body.policy, workspaceSha256: body.workspaceSha256, checkpointId: body.checkpoint.id,
          checkpointSequence: body.checkpoint.sequence,
        },
      });
      await tx.event.create({
        data: {
          runId: id, key: `approval-${approval.id}`, type: "APPROVAL_REQUESTED",
          data: {
            approval_id: approval.id, action: body.action as Prisma.InputJsonValue,
            action_sha256: body.actionSha256,
            reason: body.reason, targets: body.targets as Prisma.InputJsonValue, policy: body.policy,
            workspace_sha256: body.workspaceSha256, generation: body.generation,
            checkpoint: { id: body.checkpoint.id, sequence: body.checkpoint.sequence },
          },
        },
      });
      // The attempt stops here: no worker owns the run until a decision re-queues it.
      await tx.run.update({ where: { id }, data: { status: "WAITING_APPROVAL", leaseUntil: null } });
      return approval;
    });
  }
  @Post("runs/:id/cancel") async cancel(@Param("id") id: string) {
    return this.db.$transaction(async (tx) => {
      await tx.$queryRaw`SELECT id FROM "Run" WHERE id = ${id} FOR UPDATE`;
      const run = await tx.run.findUnique({ where: { id } });
      if (!run) throw new NotFoundException();
      if (terminal.includes(run.status)) return run;
      await tx.event.upsert({
        where: { runId_key: { runId: id, key: "cancel-request" } },
        create: {
          runId: id,
          key: "cancel-request",
          type: "CANCEL_REQUESTED",
          data: {},
        },
        update: {},
      });
      // A run that is only waiting on a person can stop immediately; a running one stops at its next boundary.
      const stopped = run.status === "QUEUED" || run.status === "WAITING_APPROVAL";
      if (stopped)
        await tx.approval.updateMany({
          where: { runId: id, status: "PENDING" },
          data: { status: "INVALIDATED", decidedAt: new Date() },
        });
      return tx.run.update({
        where: { id },
        data: {
          cancelRequested: true,
          ...(stopped ? { status: "CANCELLED", workerId: null, leaseUntil: null } : {}),
        },
      });
    });
  }
  /** The platform's own review entry: turn the review on for a run that was created without it. */
  @Post("runs/:id/review-request") async reviewRequest(@Param("id") id: string) {
    return this.db.$transaction(async (tx) => {
      await tx.$queryRaw`SELECT id FROM "Run" WHERE id = ${id} FOR UPDATE`;
      const run = await tx.run.findUnique({ where: { id } });
      if (!run) throw new NotFoundException();
      if (terminal.includes(run.status)) throw new ConflictException("任务已经结束，评审请求无法再影响它");
      if (!run.spec) throw new BadRequestException("只有仓库任务可以被评审");
      if (run.reviewPolicy === "auto") return run;
      // A claimed attempt already read its configuration, so the request has to arrive in time.
      if (run.status !== "QUEUED") throw new ConflictException("任务已被认领，评审请求对本次尝试无效");
      await tx.event.create({
        data: {
          runId: id,
          key: `review-enabled-${run.generation}`,
          type: "REVIEW_ENABLED",
          data: { reason: "用户在任务开始前要求对本次交付做独立评审。" },
        },
      });
      return tx.run.update({ where: { id }, data: { reviewPolicy: "auto" } });
    });
  }
  /** Base and candidate text of the changed files, for the read-only workbench diff. */
  @Get("runs/:id/candidate") async candidateView(@Param("id") id: string) {
    if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(id))
      throw new BadRequestException("运行标识无效");
    if (!(await this.db.run.findUnique({ where: { id } }))) throw new NotFoundException();
    const folder = path.join(process.env.ARTIFACT_ROOT || "runtime/artifacts", id);
    const readTree = (name: string) => {
      try {
        const parsed = JSON.parse(fs.readFileSync(path.join(folder, name), "utf8"));
        return parsed && typeof parsed === "object" && !Array.isArray(parsed)
          ? (parsed as Record<string, string>)
          : null;
      } catch {
        return null;
      }
    };
    const source = readTree("source.json");
    const candidate = readTree("candidate.json");
    if (!source || !candidate) throw new NotFoundException("该任务没有可对比的候选快照");
    const changed = Object.keys(source)
      .filter((name) => source[name] !== candidate[name])
      .sort();
    const base: Record<string, string> = {};
    const head: Record<string, string> = {};
    let omitted = 0;
    let characters = 0;
    for (const name of changed) {
      const before = source[name] ?? "";
      const after = candidate[name] ?? "";
      // The diff view is a reading aid, so oversized inputs are reported rather than truncated.
      if (!pathPattern.test(name) || before.length + after.length + characters > 800000) {
        omitted += 1;
        continue;
      }
      base[name] = before;
      head[name] = after;
      characters += before.length + after.length;
    }
    return { changed_files: changed, omitted_files: omitted, base, candidate: head };
  }
  @Get("runs/:id/stream") async stream(
    @Param("id") id: string,
    @Headers("last-event-id") lastId: string | undefined,
    @Query("after") after: string | undefined,
    @Req() req: any,
    @Res() reply: any,
  ) {
    if (!(await this.db.run.findUnique({ where: { id } })))
      throw new NotFoundException();
    reply.hijack();
    const res = reply.raw;
    res.writeHead(200, {
      "content-type": "text/event-stream",
      "cache-control": "no-cache",
      connection: "keep-alive",
      "x-accel-buffering": "no",
    });
    let cursor = Math.max(0, Number(lastId || after || 0) || 0),
      active = true,
      polling = false;
    res.write(": connected\n\n");
    const timer = setInterval(async () => {
      if (!active || polling) return;
      polling = true;
      try {
        const events = await this.db.event.findMany({
          where: { runId: id, id: { gt: cursor } },
          orderBy: { id: "asc" },
          take: 100,
        });
        for (const event of events) {
          if (!active) break;
          cursor = event.id;
          if (
            !res.write(`id: ${event.id}\ndata: ${JSON.stringify(event)}\n\n`)
          ) {
            res.end();
            break;
          }
        }
        if (!events.length && active) res.write(": keepalive\n\n");
      } catch {
        res.end();
      } finally {
        polling = false;
      }
    }, 800);
    res.on("close", () => {
      active = false;
      clearInterval(timer);
    });
  }
  @Post("internal/runs/:id/claim") async claim(
    @Param("id") id: string,
    @Headers("authorization") token: string,
    @Body() body: Claim,
  ) {
    this.authorize(token);
    return this.db.$transaction(async (tx) => {
      const result = await tx.run.updateMany({
        where: { id, status: "QUEUED", cancelRequested: false },
        data: {
          status: "RUNNING",
          workerId: body.workerId,
          generation: { increment: 1 },
          leaseUntil: new Date(Date.now() + 45000),
        },
      });
      if (!result.count) throw new ConflictException("任务已被认领或已结束");
      const run = await tx.run.findUniqueOrThrow({ where: { id } });
      await tx.event.create({
        data: {
          runId: id,
          key: `claimed-${run.generation}`,
          type: "RUNNING",
          data: { workerId: body.workerId, generation: run.generation },
        },
      });
      return run;
    });
  }
  @Post("internal/runs/:id/step") async step(
    @Param("id") id: string,
    @Headers("authorization") token: string,
    @Body() body: Step,
  ) {
    this.authorize(token);
    return this.db.$transaction(async (tx) => {
      await tx.$queryRaw`SELECT id FROM "Run" WHERE id = ${id} FOR UPDATE`;
      const run = await tx.run.findUniqueOrThrow({ where: { id } });
      if (
        run.workerId !== body.workerId ||
        run.generation !== body.generation ||
        !run.leaseUntil ||
        run.leaseUntil < new Date()
      )
        throw new ConflictException("运行所有权失效");
      const old = await tx.event.findUnique({
        where: { runId_key: { runId: id, key: body.key } },
      });
      if (old)
        return { cancelRequested: run.cancelRequested, status: run.status };
      if (terminal.includes(run.status))
        throw new ConflictException("任务已经结束");
      if (body.type === "CHECKPOINT_SAVED") {
        const cp = body.data;
        const awaiting = cp.phase === "awaiting_approval";
        if (body.status || run.status !== "RUNNING" || cp.generation !== run.generation ||
            typeof cp.id !== "string" || !/^[0-9a-f]{32}$/.test(cp.id) ||
            typeof cp.sha256 !== "string" || !/^[0-9a-f]{64}$/.test(cp.sha256) ||
            typeof cp.workspace_sha256 !== "string" || !/^[0-9a-f]{64}$/.test(cp.workspace_sha256) ||
            !Number.isInteger(cp.sequence) || Number(cp.sequence) < 1 ||
            !["ready", "submitted", "stopped", "awaiting_approval"].includes(String(cp.phase)) ||
            (awaiting
              ? typeof cp.action_sha256 !== "string" || !/^[0-9a-f]{64}$/.test(String(cp.action_sha256))
              : cp.action_sha256 !== null && cp.action_sha256 !== undefined))
          throw new BadRequestException("Checkpoint 元数据或执行边界无效");
        const previous = await tx.event.findFirst({
          where: { runId: id, type: "CHECKPOINT_SAVED" }, orderBy: { id: "desc" },
        });
        const previousData = previous?.data as Record<string, unknown> | undefined;
        const sequence = previousData?.generation === run.generation ? Number(previousData.sequence) : 0;
        if (cp.sequence !== sequence + 1) throw new ConflictException("Checkpoint 序号已过期");
      }
      const status =
        run.cancelRequested && body.status ? "CANCELLED" : body.status;
      await tx.event.create({
        data: {
          runId: id,
          key: body.key,
          type: body.type,
          data: body.data as Prisma.InputJsonValue,
        },
      });
      await tx.run.update({
        where: { id },
        data: {
          leaseUntil: new Date(Date.now() + 45000),
          ...(status ? { status } : {}),
          ...(status && terminal.includes(status)
            ? { result: body.data as Prisma.InputJsonValue }
            : {}),
        },
      });
      return {
        cancelRequested: run.cancelRequested,
        status: status || run.status,
      };
    });
  }
}
@Module({ controllers: [Api], providers: [Store] })
class AppModule {}
async function main() {
  const app = await NestFactory.create<NestFastifyApplication>(
    AppModule,
    new FastifyAdapter({ bodyLimit: 2 * 1024 * 1024 }),
  );
  app.useGlobalPipes(
    new ValidationPipe({
      whitelist: true,
      forbidNonWhitelisted: true,
      transform: true,
    }),
  );
  app.enableShutdownHooks();
  await app.listen(3101, "0.0.0.0");
}
void main();
