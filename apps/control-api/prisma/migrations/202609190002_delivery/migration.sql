CREATE TABLE "Delivery" (
  "id" TEXT PRIMARY KEY,
  "runId" TEXT NOT NULL REFERENCES "Run"("id") ON DELETE RESTRICT,
  "targetPath" TEXT NOT NULL,
  "targetHead" TEXT NOT NULL,
  "baseCommit" TEXT NOT NULL,
  "patchSha256" TEXT NOT NULL,
  "targetFingerprint" TEXT NOT NULL,
  "executorSha256" TEXT NOT NULL,
  "files" JSONB NOT NULL,
  "status" TEXT NOT NULL DEFAULT 'PENDING',
  "expiresAt" TIMESTAMP(3) NOT NULL,
  "decidedAt" TIMESTAMP(3),
  "receipt" JSONB,
  "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  "updatedAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT "Delivery_status_check" CHECK ("status" IN
    ('PENDING','APPROVED','REJECTED','APPLYING','APPLIED','INVALIDATED','NEEDS_ATTENTION'))
);
CREATE INDEX "Delivery_runId_createdAt_idx" ON "Delivery"("runId", "createdAt");
