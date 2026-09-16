CREATE TABLE "Run" (
  "id" TEXT NOT NULL,
  "requestKey" TEXT NOT NULL,
  "task" TEXT NOT NULL,
  "mode" TEXT NOT NULL,
  "status" TEXT NOT NULL DEFAULT 'QUEUED',
  "workerId" TEXT,
  "generation" INTEGER NOT NULL DEFAULT 0,
  "leaseUntil" TIMESTAMP(3),
  "cancelRequested" BOOLEAN NOT NULL DEFAULT false,
  "result" JSONB,
  "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  "updatedAt" TIMESTAMP(3) NOT NULL,
  CONSTRAINT "Run_pkey" PRIMARY KEY ("id"),
  CONSTRAINT "Run_mode_check" CHECK ("mode" IN ('demo','live')),
  CONSTRAINT "Run_status_check" CHECK ("status" IN ('QUEUED','RUNNING','VERIFYING','SUCCEEDED','FAILED','CANCELLED','INTERRUPTED'))
);
CREATE TABLE "Event" (
  "id" SERIAL NOT NULL,
  "runId" TEXT NOT NULL,
  "key" TEXT NOT NULL,
  "type" TEXT NOT NULL,
  "data" JSONB NOT NULL,
  "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT "Event_pkey" PRIMARY KEY ("id"),
  CONSTRAINT "Event_runId_fkey" FOREIGN KEY ("runId") REFERENCES "Run"("id") ON DELETE RESTRICT ON UPDATE CASCADE
);
CREATE TABLE "Outbox" (
  "id" SERIAL NOT NULL,
  "runId" TEXT NOT NULL,
  "publishedAt" TIMESTAMP(3),
  "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT "Outbox_pkey" PRIMARY KEY ("id")
);
CREATE UNIQUE INDEX "Run_requestKey_key" ON "Run"("requestKey");
CREATE INDEX "Run_status_leaseUntil_idx" ON "Run"("status", "leaseUntil");
CREATE UNIQUE INDEX "Event_runId_key_key" ON "Event"("runId", "key");
CREATE INDEX "Event_runId_id_idx" ON "Event"("runId", "id");
CREATE INDEX "Outbox_publishedAt_id_idx" ON "Outbox"("publishedAt", "id");
