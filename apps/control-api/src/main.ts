import "reflect-metadata";
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
  ArrayMinSize,
  ArrayMaxSize,
  ArrayUnique,
  ValidateNested,
} from "class-validator";
import { Type } from "class-transformer";
import { readFileSync, existsSync } from "node:fs";
import { timingSafeEqual } from "node:crypto";

const TASK =
  "修复 pricing.py 中 discounted_total：百分比折扣应按百分比计算，结果保留两位小数，并拒绝小于 0 或大于 100 的折扣。运行开发测试后提交补丁。";
const terminal = ["SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"];
const pathPattern = /^(?!\/)(?!.*(?:^|\/)\.\.(?:\/|$))(?!.*(?:^|\/)\.(?:\/|$))(?!.*(?:^|\/)\.git(?:\/|$))[a-zA-Z0-9_.-]+(?:\/[a-zA-Z0-9_.-]+)*$/;
class RepositorySpec {
  @Matches(/^(registered:[a-zA-Z0-9_-]{1,60}|https:\/\/github\.com\/[a-zA-Z0-9_-]+\/[a-zA-Z0-9_.-]+)$/)
  @MaxLength(200) source!: string;
  @Matches(/^[0-9a-f]{40}$/) commit!: string;
  @IsOptional() @IsString() @MaxLength(200) subdir?: string;
  @IsArray() @ArrayMinSize(1) @ArrayMaxSize(30) @ArrayUnique()
  @IsString({ each: true }) @MaxLength(240, { each: true }) @Matches(pathPattern, { each: true })
  allowedPaths!: string[];
  @IsObject() verificationFiles!: Record<string, string>;
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
  @IsOptional() @IsIn(["full", "compact"]) contextMode?: string;
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
  @IsIn(["VERIFYING", "SUCCEEDED", "FAILED", "CANCELLED"])
  status?: string;
}

@Injectable()
class Store extends PrismaClient implements OnModuleInit, OnModuleDestroy {
  connection?: ChannelModel;
  channel?: ConfirmChannel;
  timer?: NodeJS.Timeout;
  busy = false;
  async onModuleInit() {
    await this.$connect();
    this.connection = await connect(process.env.AMQP_URL!);
    this.channel = await this.connection.createConfirmChannel();
    await this.channel.assertQueue("repopilot.runs.v1", { durable: true });
    this.timer = setInterval(() => {
      void this.tick().catch((e) => console.error("coordinator:", e.message));
    }, 1000);
  }
  async onModuleDestroy() {
    clearInterval(this.timer);
    await this.channel?.close();
    await this.connection?.close();
    await this.$disconnect();
  }
  async tick() {
    if (this.busy) return;
    this.busy = true;
    try {
      for (const item of await this.outbox.findMany({
        where: { publishedAt: null },
        orderBy: { id: "asc" },
        take: 20,
      })) {
        this.channel!.sendToQueue(
          "repopilot.runs.v1",
          Buffer.from(
            JSON.stringify({
              schema_version: 1,
              message_id: String(item.id),
              run_id: item.runId,
            }),
          ),
          { persistent: true },
        );
        await this.channel!.waitForConfirms();
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
      for (const run of expired)
        await this.$transaction(async (tx) => {
          const changed = await tx.run.updateMany({
            where: {
              id: run.id,
              generation: run.generation,
              status: { in: ["RUNNING", "VERIFYING"] },
              leaseUntil: { lt: new Date() },
            },
            data: { status: "INTERRUPTED", workerId: null },
          });
          if (changed.count)
            await tx.event.create({
              data: {
                runId: run.id,
                key: `lease-expired-${run.generation}`,
                type: "INTERRUPTED",
                data: {
                  reason: "Worker 租约过期；首版明确中断，不自动重放执行。",
                },
              },
            });
        });
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
      milestone: "M0/M1",
      liveEnabled: process.env.LIVE_ENABLED === "true",
    };
  }
  @Get("runs") list() {
    return this.db.run.findMany({ orderBy: { createdAt: "desc" }, take: 40 });
  }
  @Get("baseline-tasks") baselines() {
    return catalog().map(({ id, title, task, spec }) => ({ id, title, task, spec }));
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
    const contextMode = body.contextMode || "full";
    if (spec) {
      if (!baseline && !body.task) throw new BadRequestException("指定仓库需要任务说明");
      if (body.mode === "demo" && !baseline) throw new BadRequestException("自定义仓库仅支持真实模型；预设演示仅用于内置基线");
      if ((spec.subdir && !pathPattern.test(spec.subdir)) || spec.allowedPaths.some((p: string) => p.toLowerCase().split("/").includes(".git") || p.startsWith("_repopilot_verify/")))
        throw new BadRequestException("仓库路径无效");
      const entries = Object.entries(spec.verificationFiles);
      if (!entries.length || entries.length > 20 || !entries.some(([p]) => /^test_[a-zA-Z0-9_]+\.py$/.test(p)) ||
          entries.some(([p, v]) => !pathPattern.test(p) || p.toLowerCase().split("/").includes(".git") || typeof v !== "string" || Buffer.byteLength(v) > 100000 || v.includes("\0")))
        throw new BadRequestException("验收文件必须包含 test_*.py，使用相对路径且单文件不超过 100 KB");
    } else if (body.task || contextMode !== "full") {
      throw new BadRequestException("任务说明与上下文模式需要指定仓库或基线任务");
    }
    const matches = (run: any) => run.mode === body.mode && run.task === task &&
      canonical(run.spec) === canonical(spec) && run.baselineId === (body.baselineId || null) && run.contextMode === contextMode;
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
        const run = await tx.run.create({
          data: { requestKey: body.requestKey, mode: body.mode, task,
            ...(spec ? { spec: spec as Prisma.InputJsonValue } : {}), baselineId: body.baselineId, contextMode },
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
      include: { events: { orderBy: { id: "asc" } } },
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
      return tx.run.update({
        where: { id },
        data: {
          cancelRequested: true,
          ...(run.status === "QUEUED" ? { status: "CANCELLED" } : {}),
        },
      });
    });
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
        if (body.status || run.status !== "RUNNING" || cp.generation !== run.generation ||
            typeof cp.id !== "string" || !/^[0-9a-f]{32}$/.test(cp.id) ||
            typeof cp.sha256 !== "string" || !/^[0-9a-f]{64}$/.test(cp.sha256) ||
            typeof cp.workspace_sha256 !== "string" || !/^[0-9a-f]{64}$/.test(cp.workspace_sha256) ||
            !Number.isInteger(cp.sequence) || Number(cp.sequence) < 1 ||
            !["ready", "submitted", "stopped"].includes(String(cp.phase)))
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
