import "reflect-metadata";
import {
  Body,
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
} from "class-validator";
import { timingSafeEqual } from "node:crypto";

const TASK =
  "修复 pricing.py 中 discounted_total：百分比折扣应按百分比计算，结果保留两位小数，并拒绝小于 0 或大于 100 的折扣。运行开发测试后提交补丁。";
const terminal = ["SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"];
class CreateRun {
  @IsString() @MinLength(8) @MaxLength(100) requestKey!: string;
  @IsIn(["demo", "live"]) mode!: string;
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
  @Post("runs") async create(@Body() body: CreateRun) {
    if (body.mode === "live" && process.env.LIVE_ENABLED !== "true")
      throw new ConflictException("真实模型尚未配置");
    const existing = await this.db.run.findUnique({
      where: { requestKey: body.requestKey },
    });
    if (existing) {
      if (existing.mode !== body.mode)
        throw new ConflictException("幂等键对应另一种运行模式");
      return existing;
    }
    try {
      return await this.db.$transaction(async (tx) => {
        const run = await tx.run.create({
          data: { requestKey: body.requestKey, mode: body.mode, task: TASK },
        });
        await tx.event.create({
          data: {
            runId: run.id,
            key: "created",
            type: "QUEUED",
            data: { mode: run.mode, fixture: "pricing-percent-v1" },
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
        if (run.mode !== body.mode)
          throw new ConflictException("幂等键对应另一种运行模式");
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
