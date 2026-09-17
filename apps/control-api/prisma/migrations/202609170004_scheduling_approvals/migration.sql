-- M3 reliable scheduling and approvals: recovery budget, approval policy, version-bound approvals.
ALTER TABLE "Run" ADD COLUMN "approvalPolicy" TEXT NOT NULL DEFAULT 'auto';
ALTER TABLE "Run" ADD COLUMN "recoveryAttempts" INTEGER NOT NULL DEFAULT 0;

-- A run that is waiting for a person is not running, but it is not finished either.
ALTER TABLE "Run" DROP CONSTRAINT "Run_status_check";
ALTER TABLE "Run" ADD CONSTRAINT "Run_status_check"
  CHECK ("status" IN ('QUEUED','RUNNING','VERIFYING','WAITING_APPROVAL','SUCCEEDED','FAILED','CANCELLED','INTERRUPTED'));

CREATE TABLE "Approval" (
    "id" TEXT NOT NULL,
    "runId" TEXT NOT NULL,
    "generation" INTEGER NOT NULL,
    "status" TEXT NOT NULL DEFAULT 'PENDING',
    "action" JSONB NOT NULL,
    "actionSha256" TEXT NOT NULL,
    "reason" TEXT NOT NULL,
    "targets" JSONB NOT NULL,
    "policy" TEXT NOT NULL,
    "workspaceSha256" TEXT NOT NULL,
    "checkpointId" TEXT NOT NULL,
    "checkpointSequence" INTEGER NOT NULL,
    "note" TEXT,
    "requestedAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    "decidedAt" TIMESTAMP(3),
    CONSTRAINT "Approval_pkey" PRIMARY KEY ("id")
);

CREATE UNIQUE INDEX "Approval_runId_checkpointId_actionSha256_key" ON "Approval"("runId", "checkpointId", "actionSha256");
CREATE INDEX "Approval_runId_status_idx" ON "Approval"("runId", "status");

ALTER TABLE "Approval" ADD CONSTRAINT "Approval_runId_fkey" FOREIGN KEY ("runId") REFERENCES "Run"("id") ON DELETE RESTRICT ON UPDATE CASCADE;
