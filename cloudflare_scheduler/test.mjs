import assert from "node:assert/strict";
import { minuteForHour } from "./src/index.js";

const seed = "replace-this-with-a-private-seed";
const hourA = await minuteForHour(seed, "2026-11-04-10");
const repeatedHourA = await minuteForHour(seed, "2026-11-04-10");
const hourB = await minuteForHour(seed, "2026-11-04-11");

assert.equal(hourA, repeatedHourA, "the same UTC hour must select the same minute");
assert.ok(Number.isInteger(hourA) && hourA >= 0 && hourA <= 59);
assert.ok(Number.isInteger(hourB) && hourB >= 0 && hourB <= 59);
await assert.rejects(() => minuteForHour("short", "2026-11-04-10"));
console.log({ hourA, hourB, deterministic: true });
