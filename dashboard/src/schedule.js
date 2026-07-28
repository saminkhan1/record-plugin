(function (root, factory) {
  "use strict";

  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.__HERMES_CRUNCH_SCHEDULE__ = api;
  }
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  const WEEKDAY_INDEXES = Object.freeze([0, 1, 2, 3, 4, 5, 6]);
  const DEFAULT_SCHEDULE_STATE = Object.freeze({
    mode: "interval",
    intervalValue: 30,
    intervalUnit: "minutes",
    timeOfDay: "09:00",
    weekdays: Object.freeze([1, 2, 3, 4, 5]),
    dayOfMonth: 1,
    onceAt: "",
    custom: "",
  });
  const UNIT_SUFFIX = Object.freeze({
    minutes: "m",
    hours: "h",
    days: "d",
  });
  const ENGLISH_MODE_STRINGS = Object.freeze({
    interval: "Every interval",
    daily: "Daily",
    weekly: "Weekly",
    monthly: "Monthly",
    once: "Once",
    custom: "Custom",
    intervalEvery: "Every",
    intervalUnit: "Unit",
    unitMinutes: "minutes",
    unitHours: "hours",
    unitDays: "days",
    timeOfDay: "Time",
    weekdays: "Days",
    weekdaysShort: Object.freeze(["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]),
    dayOfMonth: "Day of month",
    onceAt: "Run at",
    customLabel: "Cron expression or Hermes schedule",
    customPlaceholder: "0 9 * * 1-5",
    customHint: "Use any schedule string accepted by Hermes Cron.",
    preview: "Schedule",
    previewEmpty: "Complete the schedule above",
  });
  const ENGLISH_DESCRIBE_STRINGS = Object.freeze({
    none: "Choose a valid schedule",
    everyMinutes: "Every {n} minutes",
    everyHours: "Every {n} hours",
    everyDays: "Every {n} days",
    dailyAt: "Daily at {time}",
    weeklyAt: "Weekly on {days} at {time}",
    monthlyAt: "Monthly on the {day} at {time}",
    onceAt: "Once at {time}",
  });

  function cloneDefaultScheduleState() {
    return {
      ...DEFAULT_SCHEDULE_STATE,
      weekdays: [...DEFAULT_SCHEDULE_STATE.weekdays],
    };
  }

  function parseTimeOfDay(value) {
    if (!value || !/^\d{1,2}:\d{2}$/.test(value)) return null;
    const [rawHour, rawMinute] = value.split(":");
    const hour = Number.parseInt(rawHour, 10);
    const minute = Number.parseInt(rawMinute, 10);
    if (
      !Number.isFinite(hour)
      || !Number.isFinite(minute)
      || hour < 0
      || hour > 23
      || minute < 0
      || minute > 59
    ) {
      return null;
    }
    return { hour, minute };
  }

  function buildScheduleString(state) {
    switch (state.mode) {
      case "interval": {
        const value = Math.floor(state.intervalValue);
        const suffix = UNIT_SUFFIX[state.intervalUnit];
        if (!Number.isFinite(value) || value < 1 || !suffix) return "";
        return `every ${value}${suffix}`;
      }
      case "daily": {
        const time = parseTimeOfDay(state.timeOfDay);
        return time ? `${time.minute} ${time.hour} * * *` : "";
      }
      case "weekly": {
        const time = parseTimeOfDay(state.timeOfDay);
        if (!time) return "";
        const days = state.weekdays.length > 0
          ? [...state.weekdays].sort((left, right) => left - right).join(",")
          : "*";
        return `${time.minute} ${time.hour} * * ${days}`;
      }
      case "monthly": {
        const time = parseTimeOfDay(state.timeOfDay);
        const day = Math.floor(state.dayOfMonth);
        if (!time || !Number.isFinite(day) || day < 1 || day > 31) return "";
        return `${time.minute} ${time.hour} ${day} * *`;
      }
      case "once": {
        const value = String(state.onceAt || "").trim();
        if (!value) return "";
        return value.length === 16 ? `${value}:00` : value;
      }
      case "custom":
        return String(state.custom || "").trim();
      default:
        return "";
    }
  }

  function parseSimpleCronExpression(expression) {
    const fields = expression.trim().split(/\s+/);
    if (fields.length !== 5) return null;
    const [minuteField, hourField, dayField, monthField, weekdayField] = fields;
    if (monthField !== "*") return null;
    const literalOrList = (field) => /^\d+(,\d+)*$|^\*$/.test(field);
    if (
      !literalOrList(minuteField)
      || !literalOrList(hourField)
      || !literalOrList(dayField)
      || !literalOrList(weekdayField)
      || minuteField === "*"
      || hourField === "*"
    ) {
      return null;
    }

    const minutes = minuteField.split(",").map((value) => Number.parseInt(value, 10));
    const hours = hourField.split(",").map((value) => Number.parseInt(value, 10));
    if (
      minutes.length !== 1
      || hours.length !== 1
      || minutes[0] < 0
      || minutes[0] > 59
      || hours[0] < 0
      || hours[0] > 23
    ) {
      return null;
    }
    const time = `${String(hours[0]).padStart(2, "0")}:${String(minutes[0]).padStart(2, "0")}`;

    if (dayField === "*" && weekdayField === "*") {
      return { mode: "daily", time };
    }
    if (dayField === "*" && weekdayField !== "*") {
      const weekdays = [];
      for (const rawDay of weekdayField.split(",")) {
        const parsed = Number.parseInt(rawDay, 10);
        if (!Number.isFinite(parsed) || parsed < 0 || parsed > 7) return null;
        const day = parsed === 7 ? 0 : parsed;
        if (!weekdays.includes(day)) weekdays.push(day);
      }
      return weekdays.length > 0 ? { mode: "weekly", time, weekdays } : null;
    }
    if (dayField !== "*" && weekdayField === "*" && /^\d+$/.test(dayField)) {
      const dayOfMonth = Number.parseInt(dayField, 10);
      if (dayOfMonth >= 1 && dayOfMonth <= 31) {
        return { mode: "monthly", time, dayOfMonth };
      }
    }
    return null;
  }

  function parseScheduleString(schedule) {
    const value = String(schedule || "").trim();
    if (!value) return cloneDefaultScheduleState();
    if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?$/.test(value)) {
      return {
        ...cloneDefaultScheduleState(),
        mode: "once",
        onceAt: value.slice(0, 16),
      };
    }

    const interval = /^every\s+(\d+)\s*([mhd])$/i.exec(value);
    if (interval) {
      const suffix = interval[2].toLowerCase();
      return {
        ...cloneDefaultScheduleState(),
        mode: "interval",
        intervalValue: Math.max(1, Number.parseInt(interval[1], 10)),
        intervalUnit: suffix === "d" ? "days" : suffix === "h" ? "hours" : "minutes",
      };
    }

    const cron = parseSimpleCronExpression(value);
    if (cron) {
      return {
        ...cloneDefaultScheduleState(),
        mode: cron.mode,
        timeOfDay: cron.time,
        weekdays: cron.weekdays || [...DEFAULT_SCHEDULE_STATE.weekdays],
        dayOfMonth: cron.dayOfMonth || DEFAULT_SCHEDULE_STATE.dayOfMonth,
      };
    }
    return {
      ...cloneDefaultScheduleState(),
      mode: "custom",
      custom: value,
    };
  }

  function englishOrdinal(day) {
    const remainder100 = day % 100;
    if (remainder100 >= 11 && remainder100 <= 13) return `${day}th`;
    const suffix = day % 10 === 1 ? "st" : day % 10 === 2 ? "nd" : day % 10 === 3 ? "rd" : "th";
    return `${day}${suffix}`;
  }

  function describeScheduleBuilderState(state, cronStrings, locale = "en") {
    const scheduleModes = cronStrings?.scheduleModes || ENGLISH_MODE_STRINGS;
    const strings = {
      ...ENGLISH_DESCRIBE_STRINGS,
      ...(cronStrings?.scheduleDescribe || {}),
    };
    switch (state.mode) {
      case "interval": {
        const value = Math.floor(state.intervalValue);
        if (!Number.isFinite(value) || value < 1) return strings.none;
        const template = state.intervalUnit === "days"
          ? strings.everyDays
          : state.intervalUnit === "hours"
            ? strings.everyHours
            : strings.everyMinutes;
        return template.replace("{n}", String(value));
      }
      case "daily":
        return parseTimeOfDay(state.timeOfDay)
          ? strings.dailyAt.replace("{time}", state.timeOfDay)
          : strings.none;
      case "weekly": {
        if (!parseTimeOfDay(state.timeOfDay) || state.weekdays.length === 0) return strings.none;
        const weekdays = scheduleModes.weekdaysShort || ENGLISH_MODE_STRINGS.weekdaysShort;
        const days = [...state.weekdays]
          .sort((left, right) => left - right)
          .map((day) => weekdays[day])
          .join(", ");
        return strings.weeklyAt.replace("{days}", days).replace("{time}", state.timeOfDay);
      }
      case "monthly": {
        const day = Math.floor(state.dayOfMonth);
        if (!parseTimeOfDay(state.timeOfDay) || day < 1 || day > 31) return strings.none;
        const ordinal = locale === "en" ? englishOrdinal(day) : String(day);
        return strings.monthlyAt.replace("{day}", ordinal).replace("{time}", state.timeOfDay);
      }
      case "once":
        return state.onceAt
          ? strings.onceAt.replace("{time}", state.onceAt)
          : strings.none;
      case "custom":
        return String(state.custom || "").trim() || strings.none;
      default:
        return strings.none;
    }
  }

  function createScheduleBuilder(SDK) {
    const h = SDK.React.createElement;
    const { Button, Input, Label, Select, SelectOption } = SDK.components;

    function ScheduleBuilder({ onChange, value }) {
      const { t } = SDK.useI18n();
      const cronStrings = t?.cron || {};
      const modeStrings = {
        ...ENGLISH_MODE_STRINGS,
        ...(cronStrings.scheduleModes || {}),
      };
      const update = (patch) => onChange({ ...value, ...patch });
      const timeField = (id, label, current, onChange) => h(
        "div",
        { className: "hc-schedule-field" },
        h(Label, { htmlFor: id }, label),
        h("input", {
          id,
          type: "time",
          className: "hc-schedule-native-input",
          value: current,
          onChange: (event) => onChange(event.target.value),
        }),
      );

      let editor;
      if (value.mode === "interval") {
        editor = h(
          "div",
          { className: "hc-schedule-columns" },
          h(
            "div",
            { className: "hc-schedule-field" },
            h(Label, { htmlFor: "hc-interval-value" }, modeStrings.intervalEvery),
            h(Input, {
              id: "hc-interval-value",
              type: "number",
              min: 1,
              max: 9999,
              value: String(value.intervalValue),
              onChange: (event) => {
                const parsed = Number.parseInt(event.target.value, 10);
                update({ intervalValue: Number.isFinite(parsed) && parsed > 0 ? parsed : 1 });
              },
            }),
          ),
          h(
            "div",
            { className: "hc-schedule-field" },
            h(Label, { htmlFor: "hc-interval-unit" }, modeStrings.intervalUnit),
            h(
              Select,
              {
                id: "hc-interval-unit",
                value: value.intervalUnit,
                onValueChange: (intervalUnit) => update({ intervalUnit }),
              },
              h(SelectOption, { value: "minutes" }, modeStrings.unitMinutes),
              h(SelectOption, { value: "hours" }, modeStrings.unitHours),
              h(SelectOption, { value: "days" }, modeStrings.unitDays),
            ),
          ),
        );
      } else if (value.mode === "daily") {
        editor = timeField(
          "hc-daily-time",
          modeStrings.timeOfDay,
          value.timeOfDay,
          (timeOfDay) => update({ timeOfDay }),
        );
      } else if (value.mode === "weekly") {
        editor = h(
          SDK.React.Fragment,
          null,
          h(
            "div",
            { className: "hc-schedule-field" },
            h(Label, null, modeStrings.weekdays),
            h(
              "div",
              { className: "hc-weekdays", role: "group", "aria-label": modeStrings.weekdays },
              ...WEEKDAY_INDEXES.map((day) => {
                const selected = value.weekdays.includes(day);
                return h(
                  Button,
                  {
                    key: day,
                    type: "button",
                    size: "sm",
                    outlined: !selected,
                    "aria-pressed": selected,
                    onClick: () => update({
                      weekdays: selected
                        ? value.weekdays.filter((candidate) => candidate !== day)
                        : [...value.weekdays, day],
                    }),
                  },
                  modeStrings.weekdaysShort[day],
                );
              }),
            ),
          ),
          timeField(
            "hc-weekly-time",
            modeStrings.timeOfDay,
            value.timeOfDay,
            (timeOfDay) => update({ timeOfDay }),
          ),
        );
      } else if (value.mode === "monthly") {
        editor = h(
          "div",
          { className: "hc-schedule-columns" },
          h(
            "div",
            { className: "hc-schedule-field" },
            h(Label, { htmlFor: "hc-month-day" }, modeStrings.dayOfMonth),
            h(Input, {
              id: "hc-month-day",
              type: "number",
              min: 1,
              max: 31,
              value: String(value.dayOfMonth),
              onChange: (event) => {
                const parsed = Number.parseInt(event.target.value, 10);
                update({ dayOfMonth: parsed >= 1 && parsed <= 31 ? parsed : 1 });
              },
            }),
          ),
          timeField(
            "hc-monthly-time",
            modeStrings.timeOfDay,
            value.timeOfDay,
            (timeOfDay) => update({ timeOfDay }),
          ),
        );
      } else if (value.mode === "once") {
        editor = h(
          "div",
          { className: "hc-schedule-field" },
          h(Label, { htmlFor: "hc-once-at" }, modeStrings.onceAt),
          h("input", {
            id: "hc-once-at",
            type: "datetime-local",
            className: "hc-schedule-native-input",
            value: value.onceAt,
            onChange: (event) => update({ onceAt: event.target.value }),
          }),
        );
      } else {
        editor = h(
          "div",
          { className: "hc-schedule-field" },
          h(Label, { htmlFor: "hc-custom-schedule" }, modeStrings.customLabel),
          h(Input, {
            id: "hc-custom-schedule",
            value: value.custom,
            placeholder: modeStrings.customPlaceholder,
            onChange: (event) => update({ custom: event.target.value }),
          }),
          h("span", { className: "hc-schedule-hint" }, modeStrings.customHint),
        );
      }

      return h(
        "div",
        { className: "hc-schedule-builder" },
        h(
          "div",
          { className: "hc-schedule-field" },
          h(Label, { htmlFor: "hc-schedule-mode" }, cronStrings.scheduleMode || "Schedule"),
          h(
            Select,
            {
              id: "hc-schedule-mode",
              value: value.mode,
              onValueChange: (mode) => update({ mode }),
            },
            h(SelectOption, { value: "interval" }, modeStrings.interval),
            h(SelectOption, { value: "daily" }, modeStrings.daily),
            h(SelectOption, { value: "weekly" }, modeStrings.weekly),
            h(SelectOption, { value: "monthly" }, modeStrings.monthly),
            h(SelectOption, { value: "once" }, modeStrings.once),
            h(SelectOption, { value: "custom" }, modeStrings.custom),
          ),
        ),
        editor,
        h(
          "p",
          { className: "hc-schedule-preview" },
          h("span", null, `${modeStrings.preview}: `),
          h("code", null, buildScheduleString(value) || modeStrings.previewEmpty),
        ),
      );
    }

    return ScheduleBuilder;
  }

  return Object.freeze({
    DEFAULT_SCHEDULE_STATE,
    WEEKDAY_INDEXES,
    buildScheduleString,
    cloneDefaultScheduleState,
    createScheduleBuilder,
    describeScheduleBuilderState,
    parseScheduleString,
  });
});
