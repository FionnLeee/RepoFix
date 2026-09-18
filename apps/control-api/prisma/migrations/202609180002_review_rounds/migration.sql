-- The number of bounded revision rounds a review may ask for is adjustable per run;
-- NULL keeps the default of two.
ALTER TABLE "Run" ADD COLUMN "reviewRounds" INTEGER;
