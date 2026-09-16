import { BadRequestException, ConflictException, NotFoundException } from "@nestjs/common";
import { Prisma, PrismaClient } from "@prisma/client";
import { ArrayMaxSize, ArrayMinSize, ArrayUnique, IsArray, IsIn, IsInt, IsOptional, IsString, IsUUID, Matches, MaxLength, Min, MinLength } from "class-validator";
import { createHash } from "node:crypto";

export const hash = (text: string) => createHash("sha256").update(text).digest("hex");
export const projectKey = (source: string, subdir: string) => hash(JSON.stringify(["local", source.replace(/\.git$/, ""), subdir]));
export class MemoryWrite {
  @IsString() @MinLength(1) @MaxLength(8000) content!: string;
  @IsUUID() sourceRun!: string;
  @IsArray() @ArrayMinSize(1) @ArrayMaxSize(20) @IsInt({ each: true }) evidenceRefs!: number[];
  @IsOptional() @MaxLength(240) @Matches(/^(project|[a-zA-Z0-9_.-]+(?:\/[a-zA-Z0-9_.-]+)*)$/) scope?: string;
  @IsOptional() @IsInt() @Min(1) version?: number;
  @IsOptional() @IsIn(["active", "disabled", "deleted"]) validity?: string;
}
export class ContextOwner {
  @IsString() @MinLength(1) @MaxLength(100) workerId!: string;
  @IsInt() @Min(1) generation!: number;
}
export class IndexBegin extends ContextOwner {
  @IsUUID() id!: string;
  @IsIn(["base", "overlay"]) scope!: string;
  @Matches(/^[0-9a-f]{64}$/) embedding!: string;
  @Matches(/^[0-9a-f]{64}$/) snapshotHash!: string;
  @Matches(/^[0-9a-f]{64}$/) manifestHash!: string;
  @IsArray() @ArrayMaxSize(2000) @ArrayUnique() @IsUUID(undefined, { each: true }) pointIds!: string[];
}
export class IndexPublish extends ContextOwner {
  @IsUUID() id!: string;
}

export class ContextService {
  constructor(private db: PrismaClient) {}
  async owned(db: Prisma.TransactionClient, id: string, body: ContextOwner) {
    const run = await db.run.findUnique({ where: { id } });
    if (!run || run.status !== "RUNNING" || run.cancelRequested || run.workerId !== body.workerId ||
        run.generation !== body.generation || !run.leaseUntil || run.leaseUntil < new Date())
      throw new ConflictException("运行所有权失效");
    if (!run.projectId || !run.spec) throw new BadRequestException("需要仓库任务");
    return run;
  }
  async writeMemory(projectId: string, body: MemoryWrite, id?: string) {
    if (!body.content.trim() || body.scope?.split("/").some(p => ["..", ".", ".git"].includes(p)))
      throw new BadRequestException("记忆内容或范围无效");
    return this.db.$transaction(async tx => {
      const run = await tx.run.findUnique({ where: { id: body.sourceRun } });
      if (!run || run.projectId !== projectId || !run.spec) throw new BadRequestException("来源任务不属于该项目");
      const events = await tx.event.count({ where: { runId: run.id, id: { in: body.evidenceRefs } } });
      if (events !== new Set(body.evidenceRefs).size) throw new BadRequestException("证据事件不属于来源任务");
      const data = { projectId, content: body.content.trim(), scope: body.scope || "project",
        sourceRun: run.id, evidenceRefs: body.evidenceRefs, baseCommit: (run.spec as any).commit,
        validity: body.validity || "active", confirmedAt: new Date() };
      if (!id) {
        // Bounded explicit memory; no unbounded truncation hides relevant records at query time.
        await tx.$queryRaw`SELECT id FROM "Project" WHERE id = ${projectId} FOR UPDATE`;
        if (await tx.memory.count({ where: { projectId, validity: { not: "deleted" } } }) >= 100)
          throw new ConflictException("项目最多保留 100 条有效或停用记忆");
        return tx.memory.create({ data });
      }
      if (!body.version) throw new BadRequestException("编辑需要当前版本");
      const changed = await tx.memory.updateMany({ where: { id, projectId, version: body.version, validity: { not: "deleted" } },
        data: { ...data, version: { increment: 1 } } });
      if (!changed.count) throw new ConflictException("记忆已更新或删除，请刷新");
      return tx.memory.findUniqueOrThrow({ where: { id } });
    });
  }
  async memories(projectId: string) {
    if (!(await this.db.project.findUnique({ where: { id: projectId } }))) throw new NotFoundException();
    return this.db.memory.findMany({ where: { projectId, validity: { not: "deleted" } }, orderBy: { updatedAt: "desc" } });
  }
  async state(id: string, body: ContextOwner) {
    const run = await this.owned(this.db, id, body);
    const commit = (run.spec as any).commit;
    const memories = run.memoryEnabled ? await this.db.memory.findMany({
      where: { projectId: run.projectId!, validity: "active" }, orderBy: { updatedAt: "desc" }, take: 100,
    }) : [];
    return { compactRequested: run.compactRequested, memories: memories.map(m => ({ ...m,
      validity: m.baseCommit === commit ? "active" : "needs_review" })), memoryEnabled: run.memoryEnabled };
  }
  async begin(id: string, body: IndexBegin) {
    return this.db.$transaction(async tx => {
      await tx.$queryRaw`SELECT id FROM "Run" WHERE id = ${id} FOR UPDATE`;
      const run = await this.owned(tx, id, body);
      const commit = (run.spec as any).commit;
      const scope = body.scope === "base" ? "base" : `${id}:${run.generation}`;
      const headId = hash(JSON.stringify([run.projectId, commit, scope, body.embedding]));
      await tx.indexHead.upsert({ where: { id: headId }, update: {},
        create: { id: headId, projectId: run.projectId!, commit, scope, embedding: body.embedding } });
      await tx.$queryRaw`SELECT id FROM "IndexHead" WHERE id = ${headId} FOR UPDATE`;
      const head = await tx.indexHead.findUniqueOrThrow({ where: { id: headId } });
      const published = head.publishedId ? await tx.indexBuild.findUnique({ where: { id: head.publishedId } }) : null;
      if (published?.status === "READY" && published.snapshotHash === body.snapshotHash && published.manifestHash === body.manifestHash)
        return { ...published, head, reused: true };
      const existing = await tx.indexBuild.findUnique({ where: { id: body.id } });
      if (existing) {
        if (existing.headId !== headId || existing.runId !== id || existing.generation !== body.generation ||
            existing.snapshotHash !== body.snapshotHash || existing.manifestHash !== body.manifestHash ||
            JSON.stringify(existing.pointIds) !== JSON.stringify(body.pointIds)) throw new ConflictException("索引幂等键冲突");
        return { ...existing, head };
      }
      const build = await tx.indexBuild.create({ data: { id: body.id, headId, runId: id,
        workerId: body.workerId, generation: body.generation, snapshotHash: body.snapshotHash,
        collection: `repopilot_${body.embedding.slice(0, 24)}`, pointIds: body.pointIds, manifestHash: body.manifestHash } });
      await tx.indexHead.update({ where: { id: headId }, data: { pendingId: build.id } });
      return { ...build, head };
    });
  }
  async publish(id: string, body: IndexPublish) {
    const run = await this.owned(this.db, id, body);
    const build = await this.db.indexBuild.findUnique({ where: { id: body.id } });
    if (!build || build.runId !== id || build.workerId !== body.workerId || build.generation !== body.generation)
      throw new ConflictException("索引构建不属于该执行");
    const head = await this.db.indexHead.findUniqueOrThrow({ where: { id: build.headId } });
    if (build.status === "READY" && head.publishedId === build.id) return build;
    if (head.pendingId !== build.id) throw new ConflictException("索引构建已过期");
    const filter = { must: [
      { key: "index_id", match: { value: build.id } },
      { key: "project_id", match: { value: run.projectId } },
    ] };
    const expected = new Set(build.pointIds as string[]);
    let offset: string | undefined;
    let count = 0;
    do {
      const response = await fetch(`${process.env.QDRANT_URL || "http://qdrant:6333"}/collections/${build.collection}/points/scroll`, {
        method: "POST", headers: { "content-type": "application/json" }, signal: AbortSignal.timeout(10000),
        body: JSON.stringify({ filter, limit: 256, offset, with_payload: true, with_vector: false }),
      });
      if (!response.ok) throw new ConflictException("索引尚不可查询");
      const result = (await response.json() as any).result;
      for (const point of result.points) {
        if (!expected.delete(String(point.id)) || point.payload.commit !== head.commit ||
            point.payload.scope !== head.scope || point.payload.snapshot_hash !== build.snapshotHash ||
            point.payload.embedding !== head.embedding || point.payload.manifest_hash !== build.manifestHash)
          throw new ConflictException("索引清单或版本不匹配");
        if (++count > 2000) throw new ConflictException("索引超出上限");
      }
      offset = result.next_page_offset || undefined;
    } while (offset);
    if (expected.size) throw new ConflictException("索引尚未写入完整");
    return this.db.$transaction(async tx => {
      await tx.$queryRaw`SELECT id FROM "Run" WHERE id = ${id} FOR UPDATE`;
      await this.owned(tx, id, body);
      const changed = await tx.indexHead.updateMany({ where: { id: build.headId, pendingId: build.id },
        data: { publishedId: build.id, pendingId: null } });
      if (!changed.count) throw new ConflictException("索引构建已被替代");
      return tx.indexBuild.update({ where: { id: build.id }, data: { status: "READY" } });
    });
  }
}
