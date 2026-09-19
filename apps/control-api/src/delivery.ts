import { BadRequestException, ConflictException, NotFoundException } from "@nestjs/common";
import { Prisma, PrismaClient } from "@prisma/client";
import { createHash } from "node:crypto";
import { Type } from "class-transformer";
import {
  ArrayMaxSize, ArrayMinSize, IsArray, IsIn, IsObject, IsOptional, IsString, IsUUID, Matches, MaxLength,
  MinLength, ValidateNested,
} from "class-validator";

/**
 * Delivery = applying one finished candidate patch to a checkout the person owns.
 *
 * The control plane never touches that checkout. A host-side executor (scripts/deliver.py) surveys
 * the target, registers what it saw, the person approves exactly that, and the same executor applies
 * the patch only while the target still matches. Every state change is recorded as a run event.
 */
const BLOB = /^[0-9a-f]{40}$/;
const SHA256 = /^[0-9a-f]{64}$/;
const filePattern = /^(?!\/)(?!.*(?:^|\/)\.\.(?:\/|$))(?!.*(?:^|\/)\.(?:\/|$))(?!.*(?:^|\/)\.git(?:\/|$))[a-zA-Z0-9_.-]+(?:\/[a-zA-Z0-9_.-]+)*$/;
const APPROVAL_WINDOW_MS = 30 * 60_000;
export const terminalDeliveries = ["REJECTED", "APPLIED", "INVALIDATED", "NEEDS_ATTENTION"];

export class DeliveryFile {
  @Matches(filePattern) @MaxLength(240) path!: string;
  @IsIn(["added", "modified", "deleted"]) change!: string;
  @IsOptional() @Matches(BLOB) before?: string | null;
  @IsOptional() @Matches(BLOB) after?: string | null;
}
export class PrepareDelivery {
  @IsUUID() id!: string;
  @IsString() @MinLength(1) @MaxLength(1000) targetPath!: string;
  @Matches(BLOB) targetHead!: string;
  @Matches(BLOB) baseCommit!: string;
  @Matches(SHA256) patchSha256!: string;
  @Matches(SHA256) targetFingerprint!: string;
  @Matches(SHA256) executorSha256!: string;
  @IsArray() @ArrayMinSize(1) @ArrayMaxSize(200) @ValidateNested({ each: true }) @Type(() => DeliveryFile)
  files!: DeliveryFile[];
}
export class DeliveryDecision {
  @IsIn(["approve", "reject"]) decision!: string;
  @Matches(SHA256) patchSha256!: string;
  @Matches(SHA256) targetFingerprint!: string;
}
export class DeliveryClaim {
  @Matches(/^[0-9a-f]{64}$/) executorToken!: string;
}
export class DeliveryReceipt extends DeliveryClaim {
  @IsIn(["APPLIED", "INVALIDATED", "NEEDS_ATTENTION"]) status!: string;
  @IsObject() receipt!: Record<string, unknown>;
}

const sha = (value: string) => createHash("sha256").update(value, "utf8").digest("hex");
/** The executor's secret hash never leaves the database. */
const publicRow = ({ executorSha256, ...row }: Record<string, unknown>) => row;

/** Paths a Git patch writes, relative to the patch root, mirroring repopilot.delivery.patch_files. */
export function patchChanges(patch: string): Map<string, string> {
  const changes = new Map<string, string>();
  const lines = patch.split("\n");
  lines.forEach((line, index) => {
    if (!line.startsWith("diff --git ")) return;
    const header = /^diff --git a\/(\S+) b\/(\S+)$/.exec(line);
    if (!header) throw new BadRequestException("补丁包含无法交付的路径头");
    const [, oldPath, newPath] = header;
    let kind = "modified";
    for (const meta of lines.slice(index + 1, index + 8)) {
      if (meta.startsWith("diff --git ") || meta.startsWith("@@ ")) break;
      if (meta.startsWith("new file mode")) kind = "added";
      else if (meta.startsWith("deleted file mode")) kind = "deleted";
      else if (meta.startsWith("rename from")) kind = "renamed";
      else if (meta.startsWith("GIT binary patch") || meta.startsWith("Binary files"))
        throw new BadRequestException("二进制变更不能交付");
    }
    if (kind === "renamed") {
      changes.set(oldPath, "deleted");
      changes.set(newPath, "added");
      return;
    }
    if (oldPath !== newPath) throw new BadRequestException("补丁路径头不一致");
    changes.set(newPath, kind);
  });
  if (!changes.size) throw new BadRequestException("补丁没有改动任何文件");
  return changes;
}

export class DeliveryService {
  constructor(private db: PrismaClient) {}

  async list(runId: string) {
    if (!(await this.db.run.findUnique({ where: { id: runId } }))) throw new NotFoundException();
    const rows = await this.db.delivery.findMany({ where: { runId }, orderBy: { createdAt: "desc" } });
    return rows.map(publicRow);
  }

  async get(id: string) {
    const row = await this.db.delivery.findUnique({ where: { id } });
    if (!row) throw new NotFoundException();
    return publicRow(row);
  }

  /** Register what the executor saw on the target; nothing is written to the target yet. */
  async prepare(runId: string, body: PrepareDelivery) {
    const files = body.files.map((f) => ({ path: f.path, change: f.change, before: f.before ?? null, after: f.after ?? null }));
    for (const file of files) {
      if ((file.change === "added") !== (file.before === null) || (file.change === "deleted") !== (file.after === null))
        throw new BadRequestException("文件变更类型与前后版本不一致");
    }
    if (new Set(files.map((f) => f.path)).size !== files.length) throw new BadRequestException("交付文件重复");
    return this.db.$transaction(async (tx) => {
      await tx.$queryRaw`SELECT id FROM "Run" WHERE id = ${runId} FOR UPDATE`;
      const run = await tx.run.findUnique({ where: { id: runId } });
      if (!run) throw new NotFoundException();
      const spec = run.spec as any, result = run.result as any, evaluation = run.evaluation as any;
      if (!spec?.commit) throw new BadRequestException("只有固定版本的仓库任务可以交付");
      if (run.status !== "SUCCEEDED" || typeof result?.patch !== "string" || !result.patch)
        throw new ConflictException("只有已完成且产出非空补丁的任务可以交付");
      if (spec.commit !== body.baseCommit) throw new ConflictException("目标基线版本与任务固定版本不同");
      if (sha(result.patch) !== body.patchSha256) throw new ConflictException("补丁版本与任务结果不一致");
      const accepted = spec.verificationMode === "harness"
        ? evaluation?.status === "resolved" && evaluation.patchSha256 === body.patchSha256
        : result.verification?.passed === true;
      if (!accepted) throw new ConflictException("候选尚未通过对应的独立验收，不能交付");
      // The registered file list has to be exactly what the patch writes, prefixed by the task subdirectory.
      const prefix = spec.subdir ? `${spec.subdir}/` : "";
      const expected = new Map([...patchChanges(result.patch)].map(([p, kind]) => [prefix + p, kind]));
      const declared = new Map(files.map((f) => [f.path, f.change]));
      if (expected.size !== declared.size || [...expected].some(([p, kind]) => declared.get(p) !== kind))
        throw new ConflictException("登记的文件列表与补丁实际改动不一致");

      const existing = await tx.delivery.findUnique({ where: { id: body.id } });
      if (existing) {
        // JSONB reorders object keys, so the file list is compared field by field.
        const stored = (existing.files as any[]) ?? [];
        const sameFiles = stored.length === files.length && files.every((f, i) => f.path === stored[i]?.path &&
          f.change === stored[i]?.change && f.before === stored[i]?.before && f.after === stored[i]?.after);
        const same = existing.runId === runId && existing.targetPath === body.targetPath &&
          existing.targetHead === body.targetHead && existing.baseCommit === body.baseCommit &&
          existing.patchSha256 === body.patchSha256 && existing.targetFingerprint === body.targetFingerprint &&
          existing.executorSha256 === body.executorSha256 && sameFiles;
        if (!same) throw new ConflictException("交付 ID 已绑定其他内容");
        return publicRow(existing);
      }
      const open = await tx.delivery.findMany({
        where: { runId, targetPath: body.targetPath, status: { in: ["PENDING", "APPROVED", "APPLYING"] } },
      });
      if (open.some((row) => row.status === "APPLYING"))
        throw new ConflictException("该目标上有正在应用的交付，先让执行器完成或报告它");
      for (const stale of open) {
        // A newer survey of the same target replaces the older, still-undecided one.
        await tx.delivery.update({ where: { id: stale.id }, data: { status: "INVALIDATED", decidedAt: new Date(),
          receipt: { reason: "同一目标登记了新的快照，旧的审批请求作废", superseded_by: body.id } } });
        await tx.event.create({ data: { runId, key: `delivery-${stale.id}-INVALIDATED`, type: "DELIVERY_INVALIDATED",
          data: { delivery_id: stale.id, target: stale.targetPath, reason: "被新的目标快照取代" } } });
      }
      const row = await tx.delivery.create({ data: {
        id: body.id, runId, targetPath: body.targetPath, targetHead: body.targetHead, baseCommit: body.baseCommit,
        patchSha256: body.patchSha256, targetFingerprint: body.targetFingerprint, executorSha256: body.executorSha256,
        files: files as Prisma.InputJsonValue, expiresAt: new Date(Date.now() + APPROVAL_WINDOW_MS),
      } });
      await tx.event.create({ data: { runId, key: `delivery-${row.id}`, type: "DELIVERY_REQUESTED", data: {
        delivery_id: row.id, target: row.targetPath, target_head: row.targetHead, patch_sha256: row.patchSha256,
        target_fingerprint: row.targetFingerprint, files: files.map((f) => f.path), expires_at: row.expiresAt.toISOString(),
      } } });
      return publicRow(row);
    });
  }

  /** The person approves the exact patch and target version the page showed them. */
  decide(runId: string, id: string, body: DeliveryDecision) {
    return this.transition(id, "decide", body, runId);
  }
  /** The executor that prepared the delivery takes it before writing anything. */
  claim(id: string, body: DeliveryClaim) {
    return this.transition(id, "claim", body);
  }
  /** The executor reports what actually happened on the target. */
  finish(id: string, body: DeliveryReceipt) {
    return this.transition(id, "finish", body);
  }

  private async transition(id: string, action: "decide" | "claim" | "finish", body: any, runId?: string) {
    const outcome = await this.db.$transaction(async (tx) => {
      await tx.$queryRaw`SELECT id FROM "Delivery" WHERE id = ${id} FOR UPDATE`;
      const row = await tx.delivery.findUnique({ where: { id } });
      if (!row || (runId && row.runId !== runId)) throw new NotFoundException();
      if (action !== "decide" && sha(body.executorToken) !== row.executorSha256)
        throw new ConflictException("只有登记这次交付的执行器可以操作它");
      if (action === "decide" && (body.patchSha256 !== row.patchSha256 || body.targetFingerprint !== row.targetFingerprint))
        throw new ConflictException("页面展示的补丁或目标版本与登记内容不同，请刷新后重新核对");
      const next = action === "decide" ? (body.decision === "approve" ? "APPROVED" : "REJECTED")
        : action === "claim" ? "APPLYING" : body.status;
      // Repeating a completed step is a no-op; the executor relies on that after a crash.
      if (row.status === next || (action === "claim" && row.status === "APPLIED")) return { row };
      if (["PENDING", "APPROVED"].includes(row.status) && row.expiresAt < new Date()) {
        const expired = await tx.delivery.update({ where: { id }, data: { status: "INVALIDATED", decidedAt: new Date(),
          receipt: { reason: "审批有效期已过，未写入任何文件" } } });
        await tx.event.create({ data: { runId: row.runId, key: `delivery-${id}-INVALIDATED`, type: "DELIVERY_INVALIDATED",
          data: { delivery_id: id, target: row.targetPath, reason: "审批有效期已过" } } });
        return { row: expired, expired: true };
      }
      const allowed = action === "decide" ? row.status === "PENDING"
        : action === "claim" ? row.status === "APPROVED" : row.status === "APPLYING";
      if (!allowed) throw new ConflictException(`交付当前处于 ${row.status}，不允许此操作`);
      const updated = await tx.delivery.update({ where: { id }, data: { status: next,
        ...(action === "decide" ? { decidedAt: new Date() } : {}),
        ...(action === "finish" ? { receipt: body.receipt as Prisma.InputJsonValue } : {}) } });
      await tx.event.create({ data: { runId: row.runId, key: `delivery-${id}-${next}`, type: `DELIVERY_${next}`, data: {
        delivery_id: id, target: row.targetPath, patch_sha256: row.patchSha256, target_fingerprint: row.targetFingerprint,
        ...(action === "finish" ? { receipt: body.receipt } : {}),
      } } });
      return { row: updated };
    });
    if (outcome.expired) throw new ConflictException("交付审批已过期，请重新在目标上准备交付");
    return publicRow(outcome.row);
  }
}
