-- Independent code review is a per-run choice: "auto" reviews the candidate before the
-- acceptance run, "off" leaves the run to the coder and the tests alone.
ALTER TABLE "Run" ADD COLUMN "reviewPolicy" TEXT NOT NULL DEFAULT 'auto';
