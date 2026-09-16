CREATE TABLE "Project" (
  "id" TEXT PRIMARY KEY, "owner" TEXT NOT NULL DEFAULT 'local', "source" TEXT NOT NULL,
  "subdir" TEXT NOT NULL, "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP
);
ALTER TABLE "Run" ADD COLUMN "projectId" TEXT REFERENCES "Project"("id"),
  ADD COLUMN "memoryEnabled" BOOLEAN NOT NULL DEFAULT false,
  ADD COLUMN "compactRequested" INTEGER NOT NULL DEFAULT 0;
CREATE TABLE "Memory" (
  "id" TEXT PRIMARY KEY, "projectId" TEXT NOT NULL REFERENCES "Project"("id"),
  "scope" TEXT NOT NULL DEFAULT 'project', "content" TEXT NOT NULL, "sourceRun" TEXT NOT NULL,
  "evidenceRefs" JSONB NOT NULL, "baseCommit" TEXT NOT NULL, "validity" TEXT NOT NULL DEFAULT 'active',
  "version" INTEGER NOT NULL DEFAULT 1, "confirmedAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
  "updatedAt" TIMESTAMP(3) NOT NULL
);
CREATE INDEX "Memory_projectId_validity_idx" ON "Memory"("projectId", "validity");
CREATE TABLE "IndexHead" (
  "id" TEXT PRIMARY KEY, "projectId" TEXT NOT NULL, "commit" TEXT NOT NULL, "scope" TEXT NOT NULL,
  "embedding" TEXT NOT NULL, "pendingId" TEXT, "publishedId" TEXT, "updatedAt" TIMESTAMP(3) NOT NULL
);
CREATE INDEX "IndexHead_projectId_idx" ON "IndexHead"("projectId");
CREATE TABLE "IndexBuild" (
  "id" TEXT PRIMARY KEY, "headId" TEXT NOT NULL, "runId" TEXT NOT NULL, "workerId" TEXT NOT NULL,
  "generation" INTEGER NOT NULL, "snapshotHash" TEXT NOT NULL, "collection" TEXT NOT NULL,
  "pointIds" JSONB NOT NULL, "manifestHash" TEXT NOT NULL, "status" TEXT NOT NULL DEFAULT 'BUILDING',
  "createdAt" TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX "IndexBuild_headId_status_idx" ON "IndexBuild"("headId", "status");
