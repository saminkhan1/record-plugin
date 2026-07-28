"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {
  buildScheduleString,
  cloneDefaultScheduleState,
  describeScheduleBuilderState,
  parseScheduleString,
} = require("../src/schedule.js");

test("schedule strings round-trip through the plugin-owned serializer", () => {
  const cases = [
    ["every 45m", { mode: "interval", intervalValue: 45, intervalUnit: "minutes" }],
    ["15 8 * * *", { mode: "daily", timeOfDay: "08:15" }],
    ["30 14 * * 1,3,5", { mode: "weekly", timeOfDay: "14:30", weekdays: [1, 3, 5] }],
    ["0 9 12 * *", { mode: "monthly", timeOfDay: "09:00", dayOfMonth: 12 }],
    ["2030-02-03T14:00:00", { mode: "once", onceAt: "2030-02-03T14:00" }],
  ];

  for (const [schedule, expected] of cases) {
    const parsed = parseScheduleString(schedule);
    assert.deepEqual(
      Object.fromEntries(Object.keys(expected).map((key) => [key, parsed[key]])),
      expected,
    );
    assert.equal(buildScheduleString(parsed), schedule);
  }
});

test("unknown schedules remain editable without losing their value", () => {
  const state = parseScheduleString("0 */3 * * 1-5");
  assert.equal(state.mode, "custom");
  assert.equal(state.custom, "0 */3 * * 1-5");
  assert.equal(buildScheduleString(state), "0 */3 * * 1-5");
});

test("default state clones do not share weekday mutations", () => {
  const first = cloneDefaultScheduleState();
  const second = cloneDefaultScheduleState();
  first.weekdays.pop();
  assert.deepEqual(second.weekdays, [1, 2, 3, 4, 5]);
});

test("weekly schedules require a visible day selection in the review summary", () => {
  const state = {
    ...cloneDefaultScheduleState(),
    mode: "weekly",
    weekdays: [],
  };
  assert.equal(
    describeScheduleBuilderState(state, {
      scheduleDescribe: { none: "Choose a valid schedule" },
      scheduleModes: { weekdaysShort: ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"] },
    }),
    "Choose a valid schedule",
  );
});
