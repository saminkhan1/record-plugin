import {
  Badge,
  Button,
  ConfirmDialog,
  CopyButton,
  CronScheduleField,
  CronScheduleSummary,
  Input,
  Loader,
  PALETTE_AREA,
  ROUTES_AREA,
  SegmentedControl,
  SIDEBAR_NAV_AREA,
  STATUSBAR_AREAS,
  StatusDot,
  Textarea,
  host,
  usePluginI18n,
  useQuery,
  useQueryClient,
  useValue
} from '@hermes/plugin-sdk'
import { useEffect, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'record'
const ROUTE = '/workflows'
const POLL_MS = 5_000
const ACTIVE_POLL_MS = 3_000
const DRAFT_POLL_MS = 3_000
const DEFAULT_SCHEDULE = '0 9 * * 1-5'
const REVIEW_STATES = new Set([
  'needs_input',
  'needs_rerecord',
  'ready_to_redraft',
  'ready_to_schedule'
])

function element(type, props = {}, children) {
  const { key, ...componentProps } = props

  if (Array.isArray(children)) {
    return jsxs(type, { ...componentProps, children }, key)
  }

  return jsx(type, { ...componentProps, children }, key)
}

function errorMessage(error, fallback = 'Hermes could not complete that action.') {
  const raw = error instanceof Error ? error.message : String(error || fallback)
  const jsonStart = raw.indexOf('{')

  if (jsonStart >= 0) {
    try {
      const parsed = JSON.parse(raw.slice(jsonStart))

      if (typeof parsed?.detail === 'string' && parsed.detail.trim()) {
        return parsed.detail.trim()
      }
      if (Array.isArray(parsed?.detail)) {
        const details = parsed.detail
          .map(item => String(item?.msg || '').trim())
          .filter(Boolean)

        if (details.length) {
          return details.join(' ')
        }
      }
    } catch {
      // The backend error was not a JSON envelope; keep its original message.
    }
  }

  return raw || fallback
}

function recordingKey(recording) {
  return String(recording?.recording_key || recording?.recording_dir || '')
}

function reviewSteps(draft) {
  const review = draft?.workflow?.review

  return Array.isArray(review?.execution_steps) ? review.execution_steps : []
}

function reviewBlockers(draft, review) {
  if (Array.isArray(review?.blockers)) {
    return review.blockers
  }

  return Array.isArray(draft?.review_blockers) ? draft.review_blockers : []
}

function blockerText(blocker) {
  if (typeof blocker === 'string') {
    return blocker
  }

  return String(blocker?.message || blocker?.description || blocker?.kind || 'Review this workflow detail.')
}

function reviewStateText(t, value) {
  const state = String(value || '')

  return REVIEW_STATES.has(state) ? t(`review.states.${state}`) : state
}

function reviewMessages(review) {
  const threads = Array.isArray(review?.threads) ? review.threads : []

  return threads.flatMap((thread, threadIndex) => {
    const messages = Array.isArray(thread?.messages) ? thread.messages : []

    return messages.map((message, messageIndex) => ({
      ...message,
      key: `${thread?.thread_id || threadIndex}:${messageIndex}`
    }))
  })
}

function webhookTrigger(draft) {
  const trigger = draft?.workflow?.trigger

  return trigger && typeof trigger === 'object' ? trigger : {}
}

function TriggerModeControl({ disabled, value, onChange }) {
  const t = usePluginI18n(ID)

  return element('div', { className: 'flex flex-col gap-1.5 text-xs' }, [
    element(
      'span',
      {
        className: 'font-medium text-(--ui-text-secondary)',
        id: 'new-workflow-trigger-label',
        key: 'label'
      },
      t('trigger.label')
    ),
    element(SegmentedControl, {
      'aria-labelledby': 'new-workflow-trigger-label',
      disabled: Boolean(disabled),
      key: 'control',
      onChange,
      options: [
        { id: 'schedule', label: t('trigger.schedule') },
        { id: 'webhook', label: t('trigger.webhook') }
      ],
      value
    })
  ])
}

function WebhookReadiness({ status, draft }) {
  const t = usePluginI18n(ID)
  const trigger = webhookTrigger(draft)
  const supported = trigger.feasibility === 'supported'
  const receiverReady = Boolean(status?.ready)
  const events = Array.isArray(trigger.events) ? trigger.events.filter(Boolean) : []
  const reason = String(status?.reason || '')

  return element(
    Notice,
    { kind: supported && receiverReady ? 'success' : 'warn' },
    [
      element(
        'strong',
        { className: 'font-medium', key: 'title' },
        draft
          ? supported
            ? t('trigger.detected')
            : t('trigger.needsDefinition')
          : t('trigger.webhookTitle')
      ),
      element(
        'p',
        { className: 'mt-1', key: 'description' },
        draft
          ? String(trigger.description || trigger.reason || t('trigger.missingDefinition'))
          : t('trigger.webhookDescription')
      ),
      events.length
        ? element(
            'p',
            { className: 'mt-1 text-(--ui-text-tertiary)', key: 'events' },
            t('trigger.events', events.join(', '))
          )
        : null,
      element(
        'p',
        { className: 'mt-1 text-(--ui-text-tertiary)', key: 'receiver' },
        receiverReady
          ? t('trigger.receiverReady')
          : reason || t('trigger.receiverNotReady')
      )
    ]
  )
}

function normalizeCaptureAuthorization(value) {
  const authorization = value && typeof value === 'object' ? value : {}
  const nonce = String(
    authorization.capture_authorization_nonce ||
      authorization.nonce ||
      authorization.authorization_nonce ||
      authorization.capture_authorization?.capture_authorization_nonce ||
      authorization.capture_authorization?.nonce ||
      ''
  ).trim()

  if (!nonce) {
    throw new Error('Hermes did not return a Chrome capture authorization.')
  }

  return { authorization, nonce }
}

function createClient(ctx) {
  const get = (path, timeoutMs = 30_000) => ctx.rest(path, { timeoutMs })
  const post = (path, body, timeoutMs = 60_000) =>
    ctx.rest(path, { method: 'POST', body, timeoutMs })

  return {
    scope: ctx.source,
    activeRecording(profile) {
      return get(`/recordings/active?profile=${encodeURIComponent(profile)}`)
    },
    webhookStatus(profile) {
      return get(`/webhook-status?profile=${encodeURIComponent(profile)}`)
    },
    async startRecording(input) {
      const provisioned = await post(
        '/recordings/capture-authorization',
        {
          profile: input.profile,
          name: input.name,
          instructions: input.instructions,
          schedule: input.schedule,
          run_mode: input.runMode
        },
        120_000
      )
      const { authorization, nonce } = normalizeCaptureAuthorization(provisioned)

      return post(
        '/recordings/start-desktop',
        {
          profile: input.profile,
          name: input.name,
          instructions: input.instructions,
          schedule: input.schedule,
          run_mode: input.runMode,
          capture_authorization_nonce: nonce,
          capture_authorization: authorization
        },
        120_000
      )
    },
    stopRecording(profile, key) {
      return post(
        `/recordings/${encodeURIComponent(key)}/stop-desktop`,
        { profile },
        120_000
      )
    },
    discardRecording(profile, key) {
      return post(`/recordings/${encodeURIComponent(key)}/discard`, { profile })
    },
    startDraft({ profile, key, name, instructions, schedule, expectedDigest, reviewThreadId }) {
      const body = {
        profile,
        name,
        instructions,
        schedule
      }

      if (expectedDigest) {
        body.expected_draft_digest = expectedDigest
      }
      if (reviewThreadId) {
        body.review_thread_id = reviewThreadId
      }

      return post(`/recordings/${encodeURIComponent(key)}/draft`, body)
    },
    draftStatus(operationId) {
      return get(`/drafts/${encodeURIComponent(operationId)}`)
    },
    cancelDraft(operationId) {
      return post(`/drafts/${encodeURIComponent(operationId)}/cancel`, {})
    },
    loadReview(profile, key, digest) {
      return get(
        `/recordings/${encodeURIComponent(key)}/review-conversation` +
          `?profile=${encodeURIComponent(profile)}` +
          `&expected_draft_digest=${encodeURIComponent(digest)}`
      )
    },
    sendReview(profile, key, digest, message) {
      return post(
        `/recordings/${encodeURIComponent(key)}/review-conversation`,
        {
          profile,
          expected_draft_digest: digest,
          message
        },
        120_000
      )
    },
    setHermesJob({ profile, recordingDir, schedule, runMode, digest }) {
      return post(
        '/recordings/set-job',
        {
          profile,
          recording_dir: recordingDir,
          schedule,
          run_mode: runMode,
          expected_draft_digest: digest
        },
        120_000
      )
    }
  }
}

function Field({ children, error, errorId, hint, hintId, label }) {
  return element('label', { className: 'flex flex-col gap-1.5 text-xs' }, [
    element(
      'span',
      { className: 'font-medium text-(--ui-text-secondary)', key: 'label' },
      label
    ),
    element('span', { key: 'control' }, children),
    hint
      ? element(
          'span',
          {
            className: 'text-[0.6875rem] leading-4 text-(--ui-text-tertiary)',
            id: hintId,
            key: 'hint'
          },
          hint
        )
      : null,
    error
      ? element(
          'span',
          { className: 'text-[0.6875rem] leading-4 text-destructive', id: errorId, key: 'error', role: 'alert' },
          error
        )
      : null
  ])
}

function Panel({ children, className = '', title, description, action }) {
  return element(
    'section',
    {
      className: `min-w-0 ${className}`.trim()
    },
    [
      element(
        'header',
        {
          className: 'flex items-start justify-between gap-3',
          key: 'header'
        },
        [
          element('div', { className: 'min-w-0', key: 'copy' }, [
            element(
              'h2',
              { className: 'text-sm font-semibold text-(--ui-text-primary)', key: 'title' },
              title
            ),
            description
              ? element(
                  'p',
                  {
                    className: 'mt-0.5 text-xs leading-5 text-(--ui-text-tertiary)',
                    key: 'description'
                  },
                  description
                )
              : null
          ]),
          action ? element('div', { className: 'shrink-0', key: 'action' }, action) : null
        ]
      ),
      element('div', { className: 'mt-3', key: 'body' }, children)
    ]
  )
}

function Notice({ children, kind = 'info', live = false }) {
  const classes = {
    error: 'border-destructive/40 bg-destructive/5 text-destructive',
    info: 'border-(--ui-stroke-secondary) bg-(--ui-bg-tertiary) text-(--ui-text-secondary)',
    success: 'border-primary/30 bg-primary/5 text-(--ui-text-secondary)',
    warn: 'border-(--ui-accent) bg-(--ui-bg-tertiary) text-(--ui-text-secondary)'
  }

  return element(
    'div',
    {
      className: `select-text rounded-[4px] border px-3 py-2 text-xs leading-5 ${classes[kind] || classes.info}`,
      role: kind === 'error' ? 'alert' : live ? 'status' : undefined
    },
    children
  )
}

function WorkflowForm({ form, setForm, busy, onStart }) {
  const t = usePluginI18n(ID)
  const [errors, setErrors] = useState({})
  const nameRef = useRef(null)
  const instructionsRef = useRef(null)
  const scheduleRef = useRef(null)

  function clearError(field) {
    setErrors(current => {
      if (!current[field]) {
        return current
      }

      const next = { ...current }
      delete next[field]
      return next
    })
  }

  function validate() {
    const next = {}

    if (!form.name.trim()) {
      next.name = t('errors.nameRequired')
    }
    if (!form.instructions.trim()) {
      next.instructions = t('errors.instructionsRequired')
    }
    if (form.runMode === 'schedule' && !form.schedule.trim()) {
      next.schedule = t('errors.scheduleRequired')
    }

    setErrors(next)

    const firstInvalid = [
      ['name', nameRef],
      ['instructions', instructionsRef],
      ['schedule', scheduleRef]
    ].find(([field]) => next[field])

    firstInvalid?.[1].current?.focus()
    return next
  }

  return element(
    Panel,
    {
      title: t('new.title'),
      description: t('new.description')
    },
    element(
      'form',
      {
        className: 'flex flex-col gap-4',
        onSubmit: event => {
          event.preventDefault()

          if (busy || Object.keys(validate()).length) {
            return
          }

          void onStart()
        }
      },
      [
        element(
          Field,
          {
            error: errors.name,
            errorId: 'new-workflow-name-error',
            key: 'name',
            label: t('new.name')
          },
          element(Input, {
            'aria-describedby': errors.name ? 'new-workflow-name-error' : undefined,
            'aria-invalid': errors.name ? 'true' : undefined,
            autoComplete: 'off',
            disabled: Boolean(busy),
            id: 'new-workflow-name',
            onChange: event => {
              clearError('name')
              setForm(current => ({ ...current, name: event.target.value }))
            },
            placeholder: t('new.namePlaceholder'),
            ref: nameRef,
            value: form.name
          })
        ),
        element(
          Field,
          {
            error: errors.instructions,
            errorId: 'new-workflow-instructions-error',
            hint: t('new.instructionsHint'),
            hintId: 'new-workflow-instructions-hint',
            key: 'instructions',
            label: t('new.instructions')
          },
          element(Textarea, {
            'aria-describedby': [
              'new-workflow-instructions-hint',
              errors.instructions ? 'new-workflow-instructions-error' : null
            ]
              .filter(Boolean)
              .join(' '),
            'aria-invalid': errors.instructions ? 'true' : undefined,
            className: 'min-h-28 resize-y',
            disabled: Boolean(busy),
            id: 'new-workflow-instructions',
            onChange: event => {
              clearError('instructions')
              setForm(current => ({ ...current, instructions: event.target.value }))
            },
            placeholder: t('new.instructionsPlaceholder'),
            ref: instructionsRef,
            value: form.instructions
          })
        ),
        element(TriggerModeControl, {
          disabled: Boolean(busy),
          key: 'mode',
          onChange: runMode => {
            clearError('schedule')
            setForm(current => ({ ...current, runMode }))
          },
          value: form.runMode
        }),
        form.runMode === 'schedule'
          ? element(CronScheduleField, {
              controlRef: scheduleRef,
              description: t('new.scheduleHint'),
              disabled: Boolean(busy),
              error: errors.schedule,
              idPrefix: 'new-workflow',
              key: 'schedule',
              label: t('new.schedule'),
              onChange: schedule => {
                clearError('schedule')
                setForm(current => ({ ...current, schedule }))
              },
              routineOnly: true,
              value: form.schedule
            })
          : element(
              'p',
              {
                className:
                  'rounded-[4px] bg-(--ui-bg-tertiary) px-3 py-2 text-xs leading-5 text-(--ui-text-tertiary)',
                key: 'event-hint'
              },
              t('trigger.eventHint')
            ),
        element('div', { className: 'flex justify-end', key: 'submit' }, [
          element(
            Button,
            {
              disabled: Boolean(busy),
              key: 'button',
              type: 'submit'
            },
            busy === 'start' ? t('new.starting') : t('new.start')
          )
        ])
      ]
    )
  )
}

function ActiveRecording({ recording, busy, operation, onCancelDraft, onDiscard, onStopAndDraft }) {
  const t = usePluginI18n(ID)
  const active = recording.status === 'recording'
  const progress = Array.isArray(operation?.progress) ? operation.progress : []
  const latest = progress.at(-1)
  const progressText = latest?.text || latest?.tool || t('recording.drafting')

  return element(
    Panel,
    {
      title: active ? t('recording.activeTitle') : t('recording.savedTitle'),
      description: active ? t('recording.activeDescription') : t('recording.savedDescription'),
      action: element(
        Badge,
        { variant: active ? 'default' : 'muted' },
        String(recording.status || 'saved')
      )
    },
    element('div', { className: 'flex flex-col gap-4' }, [
      element('div', { className: 'flex items-start gap-3', key: 'status' }, [
        element('span', {
          'aria-hidden': 'true',
          className:
            'mt-1.5 size-2 shrink-0 rounded-full ' +
            (active ? 'animate-pulse bg-destructive' : 'bg-(--ui-text-quaternary)'),
          key: 'dot'
        }),
        element('div', { className: 'min-w-0', key: 'copy' }, [
          element(
            'div',
            { className: 'text-sm font-medium text-(--ui-text-primary)', key: 'name' },
            recording.name
          ),
          element(
            'p',
            { className: 'mt-1 text-xs leading-5 text-(--ui-text-tertiary)', key: 'instructions' },
            recording.instructions
          )
        ])
      ]),
      busy === 'draft'
        ? element(Notice, { key: 'progress', live: true }, [
            element(Loader, {
              className: 'mr-2 inline-flex size-5 align-middle',
              key: 'loader',
              label: progressText,
              type: 'thinking-five'
            }),
            progressText
          ])
        : null,
      element('div', { className: 'flex flex-wrap justify-end gap-2', key: 'actions' }, [
        element(
          Button,
          {
            disabled: Boolean(busy),
            key: 'discard',
            onClick: () => void onDiscard(),
            variant: 'text'
          },
          t('recording.discard')
        ),
        busy === 'draft'
          ? element(
              Button,
              {
                key: 'cancel',
                onClick: () => void onCancelDraft(),
                variant: 'outline'
              },
              t('recording.cancelDraft')
            )
          : element(
              Button,
              {
                disabled: Boolean(busy),
                key: 'draft',
                onClick: () => void onStopAndDraft()
              },
              active ? t('recording.stopAndDraft') : t('recording.retryDraft')
            )
      ])
    ])
  )
}

function ReviewConversation({
  review,
  message,
  setMessage,
  busy,
  onSend,
  onRedraft
}) {
  const t = usePluginI18n(ID)
  const messages = reviewMessages(review)
  const canRedraft = review?.state === 'ready_to_redraft' && review?.thread_id

  return element('div', { className: 'flex min-h-0 flex-col gap-3' }, [
    element(
      'div',
      {
        className:
          'flex max-h-72 min-h-24 flex-col gap-2 overflow-y-auto rounded-[4px] border border-(--ui-stroke-secondary) p-2',
        key: 'messages'
      },
      messages.length
        ? messages.map(item =>
            element(
              'div',
              {
                className:
                  'max-w-[92%] rounded-[4px] px-2.5 py-2 text-xs leading-5 ' +
                  (item.role === 'user'
                    ? 'ml-auto bg-primary/10 text-(--ui-text-primary)'
                    : 'mr-auto bg-(--ui-bg-tertiary) text-(--ui-text-secondary)'),
                key: item.key
              },
              item.content
            )
          )
        : element(
            'p',
            { className: 'm-auto text-xs text-(--ui-text-tertiary)' },
            review ? t('review.noMessages') : t('review.loading')
          )
    ),
    element(
      'label',
      { className: 'text-xs font-medium text-(--ui-text-secondary)', htmlFor: 'review-message', key: 'label' },
      t('review.messageLabel')
    ),
    element(Textarea, {
      className: 'min-h-20 resize-y',
      disabled: Boolean(busy) || !review,
      id: 'review-message',
      key: 'input',
      maxLength: 4_000,
      onChange: event => setMessage(event.target.value),
      placeholder: t('review.messagePlaceholder'),
      value: message
    }),
    element('div', { className: 'flex flex-wrap justify-end gap-2', key: 'actions' }, [
      element(
        Button,
        {
          disabled: Boolean(busy) || !message.trim() || !review,
          key: 'send',
          onClick: () => void onSend(),
          variant: 'outline'
        },
        busy === 'review' ? t('review.sending') : t('review.send')
      ),
      canRedraft
        ? element(
            Button,
            {
              disabled: Boolean(busy),
              key: 'redraft',
              onClick: () => void onRedraft()
            },
            busy === 'draft' ? t('review.updating') : t('review.update')
          )
        : null
    ])
  ])
}

function WorkflowReview({
  draft,
  review,
  runMode,
  schedule,
  webhookStatus,
  busy,
  message,
  setMessage,
  onSendReview,
  onRedraft,
  onSetJob
}) {
  const t = usePluginI18n(ID)
  const steps = reviewSteps(draft)
  const blockers = reviewBlockers(draft, review)
  const messages = reviewMessages(review)
  const canRedraft = review?.state === 'ready_to_redraft' && review?.thread_id
  const trigger = webhookTrigger(draft)
  const webhookReady =
    runMode !== 'webhook' ||
    (trigger.feasibility === 'supported' && Boolean(webhookStatus?.ready))
  const ready =
    review?.state === 'ready_to_schedule' &&
    blockers.length === 0 &&
    webhookReady

  return element(
    Panel,
    {
      title: t('review.title'),
      description: t('review.description'),
      action: element(
        Badge,
        { variant: ready ? 'default' : blockers.length ? 'warn' : 'muted' },
        ready
          ? t('review.ready')
          : reviewStateText(t, review?.state) || t('review.checking')
      )
    },
    element('div', { className: 'flex flex-col gap-5' }, [
      element('div', { className: 'flex min-w-0 flex-col gap-4', key: 'plan' }, [
        element('div', { key: 'summary' }, [
          element(
            'div',
            {
              className: 'mb-2 text-xs font-medium text-(--ui-text-secondary)',
              key: 'heading'
            },
            t('review.steps')
          ),
          steps.length
            ? element(
                'ol',
                { className: 'flex flex-col gap-2' },
                steps.map((step, index) =>
                  element('li', { className: 'flex gap-2 text-xs leading-5', key: `${index}:${step}` }, [
                    element(
                      'span',
                      {
                        className:
                          'mt-0.5 flex size-4 shrink-0 items-center justify-center rounded-[3px] bg-(--ui-bg-quaternary) text-[0.6rem] text-(--ui-text-tertiary)',
                        key: 'number'
                      },
                      index + 1
                    ),
                    element(
                      'span',
                      { className: 'text-(--ui-text-secondary)', key: 'text' },
                      step
                    )
                  ])
                )
              )
            : element(Notice, { kind: 'warn' }, t('review.noSteps'))
        ]),
        blockers.length
          ? element(Notice, { key: 'blockers', kind: 'warn' }, [
              element('strong', { className: 'font-medium', key: 'title' }, t('review.needsAttention')),
              element(
                'ul',
                { className: 'mt-1 list-disc space-y-1 pl-4', key: 'list' },
                blockers.map((blocker, index) =>
                  element('li', { key: index }, blockerText(blocker))
                )
              )
            ])
          : null,
        element('div', { className: 'grid gap-1.5', key: 'starts' }, [
          element(
            'div',
            { className: 'text-xs font-medium text-(--ui-text-secondary)', key: 'label' },
            t('review.starts')
          ),
          runMode === 'schedule'
            ? element(CronScheduleSummary, { key: 'schedule', value: schedule })
            : element(WebhookReadiness, {
                draft,
                key: 'webhook',
                status: webhookStatus
              })
        ]),
        element('div', { className: 'flex justify-end', key: 'set' }, [
          element(
            Button,
            {
              disabled:
                !ready ||
                (runMode === 'schedule' && !schedule.trim()) ||
                Boolean(busy),
              onClick: () => void onSetJob()
            },
            busy === 'install' ? t('review.turningOn') : t('review.turnOn')
          )
        ])
      ]),
      element('details', { className: 'group', key: 'conversation', open: Boolean(messages.length || canRedraft) }, [
        element(
          'summary',
          {
            className:
              'cursor-pointer text-xs font-medium text-(--ui-text-secondary) hover:text-(--ui-text-primary)',
            key: 'summary'
          },
          t('review.conversation')
        ),
        element(
          'div',
          { className: 'mt-3 max-w-2xl', key: 'content' },
          element(ReviewConversation, {
            busy,
            key: 'conversation',
            message,
            onRedraft,
            onSend: onSendReview,
            review,
            setMessage
          })
        )
      ])
    ])
  )
}

function InstalledJob({ installed, onNew }) {
  const t = usePluginI18n(ID)
  const jobId = String(
    installed?.job_id ||
      installed?.job?.id ||
      installed?.install?.job_id ||
      ''
  )
  const webhook =
    installed?.webhook && typeof installed.webhook === 'object'
      ? installed.webhook
      : null

  return element(
    Panel,
    {
      title: t('installed.title'),
      description: webhook
        ? t('installed.webhookJobDescription')
        : t('installed.description'),
      action: element(
        Badge,
        {},
        webhook ? t('installed.awaitingEvent') : t('installed.active')
      )
    },
    element('div', { className: 'flex flex-col gap-4' }, [
      jobId
        ? element('div', { className: 'text-xs', key: 'id' }, [
            element(
              'span',
              { className: 'text-(--ui-text-tertiary)', key: 'label' },
              t('installed.jobId')
            ),
            element(
              'code',
              {
                className:
                  'select-text ml-2 rounded-[3px] bg-(--ui-bg-tertiary) px-1.5 py-0.5 text-(--ui-text-secondary)',
                key: 'value'
              },
              jobId
            )
          ])
        : null,
      webhook?.url
        ? element(Notice, { key: 'webhook', kind: 'success' }, [
            element(
              'strong',
              { className: 'font-medium', key: 'title' },
              t('installed.webhookReady')
            ),
            element(
              'p',
              { className: 'mt-1', key: 'description' },
              t('installed.webhookDescription')
            ),
            element('div', { className: 'mt-3 flex items-center gap-2', key: 'url' }, [
              element(
                'code',
                {
                  className:
                    'select-text min-w-0 flex-1 break-all rounded-[3px] bg-(--ui-bg-tertiary) px-2 py-1 text-[0.6875rem] text-(--ui-text-secondary)',
                  key: 'value'
                },
                webhook.url
              ),
              element(CopyButton, {
                key: 'copy',
                label: t('installed.copyUrl'),
                text: webhook.url
              })
            ]),
            webhook.secret
              ? element('details', { className: 'mt-3', key: 'secret' }, [
                  element(
                    'summary',
                    {
                      className:
                        'cursor-pointer text-xs font-medium text-(--ui-text-tertiary)',
                      key: 'summary'
                    },
                    t('installed.signingSecret')
                  ),
                  element(
                    'p',
                    { className: 'mt-1 text-[0.6875rem] text-(--ui-text-tertiary)', key: 'warning' },
                    t('installed.secretWarning')
                  ),
                  element('div', { className: 'mt-2 flex items-center gap-2', key: 'secret-row' }, [
                    element(
                      'code',
                      {
                        className:
                          'select-text min-w-0 flex-1 break-all rounded-[3px] bg-(--ui-bg-tertiary) px-2 py-1 text-[0.6875rem] text-(--ui-text-secondary)',
                        key: 'value'
                      },
                      webhook.secret
                    ),
                    element(CopyButton, {
                      key: 'copy',
                      label: t('installed.copySecret'),
                      text: webhook.secret
                    })
                  ])
                ])
              : null
          ])
        : null,
      element('div', { className: 'flex flex-wrap justify-end gap-2', key: 'actions' }, [
        element(
          Button,
          {
            key: 'new',
            onClick: onNew,
            variant: 'outline'
          },
          t('installed.new')
        ),
        element(
          Button,
          {
            key: 'cron',
            onClick: () => host.navigate('/cron')
          },
          t('installed.manage')
        )
      ])
    ])
  )
}

function RecordProfilePage({ client, profile }) {
  const t = usePluginI18n(ID)
  const queryClient = useQueryClient()
  const [form, setForm] = useState({
    name: '',
    instructions: '',
    schedule: DEFAULT_SCHEDULE,
    runMode: 'schedule'
  })
  const [busy, setBusy] = useState('')
  const [notice, setNotice] = useState('')
  const [operationId, setOperationId] = useState('')
  const [reviewMessage, setReviewMessage] = useState('')
  const [discardOpen, setDiscardOpen] = useState(false)
  const [installed, setInstalled] = useState(null)
  const [recording, setRecording] = useState(null)
  const [draft, setDraft] = useState(null)
  const processedOperation = useRef('')
  const seededReview = useRef('')
  const activeRecordingKey = [client.scope, 'active-recording', profile]

  const activeRecordingQuery = useQuery({
    queryKey: activeRecordingKey,
    queryFn: () => client.activeRecording(profile),
    refetchInterval: query => {
      const status = query.state.data?.recording?.status

      return status === 'recording' ? ACTIVE_POLL_MS : false
    },
    retry: false
  })

  const key = recordingKey(recording)
  const digest = String(draft?.draft_digest || '')
  const reviewKey = [client.scope, 'review', profile, key, digest]

  const draftQuery = useQuery({
    enabled: Boolean(operationId),
    queryKey: [client.scope, 'draft-operation', profile, operationId],
    queryFn: () => client.draftStatus(operationId),
    refetchInterval: query => {
      const status = query.state.data?.status

      return ['completed', 'failed', 'canceled'].includes(status) ? false : DRAFT_POLL_MS
    },
    retry: false
  })

  const reviewQuery = useQuery({
    enabled: Boolean(key && digest),
    queryKey: reviewKey,
    queryFn: () => client.loadReview(profile, key, digest),
    refetchInterval: false,
    retry: false
  })

  const webhookStatusQuery = useQuery({
    enabled: Boolean(draft && form.runMode === 'webhook'),
    queryKey: [client.scope, 'webhook-status', profile],
    queryFn: () => client.webhookStatus(profile),
    refetchInterval: POLL_MS,
    retry: false
  })
  const webhookStatus = webhookStatusQuery.error
    ? { ready: false, reason: errorMessage(webhookStatusQuery.error) }
    : webhookStatusQuery.data

  useEffect(() => {
    const active = activeRecordingQuery.data?.recording

    if (active?.status === 'recording') {
      setRecording(active)
    }
  }, [activeRecordingQuery.data?.recording?.recording_key])

  useEffect(() => {
    if (!recording) {
      return
    }

    setForm(current => ({
      name: String(recording.name || current.name),
      instructions: String(recording.instructions || current.instructions),
      schedule:
        recording.schedule && recording.schedule !== 'triggered'
          ? String(recording.schedule)
          : current.schedule,
      runMode:
        recording.run_mode === 'webhook' || recording.schedule === 'triggered'
          ? 'webhook'
          : current.runMode
    }))
  }, [key])

  useEffect(() => {
    const operation = draftQuery.data

    if (!operation || !['completed', 'failed', 'canceled'].includes(operation.status)) {
      return
    }
    const token = `${operation.operation_id}:${operation.status}`

    if (processedOperation.current === token) {
      return
    }
    processedOperation.current = token
    setBusy('')
    setOperationId('')

    if (operation.status === 'completed' && operation.result) {
      setRecording(current => (current ? { ...current, status: 'drafted' } : current))
      setDraft(operation.result)
      setNotice('')
      return
    }

    setNotice(
      operation.status === 'canceled'
        ? t('errors.draftCanceled')
        : operation.error || t('errors.draftFailed')
    )
  }, [draftQuery.data?.operation_id, draftQuery.data?.status])

  useEffect(() => {
    const review = reviewQuery.data

    if (!review?.needs_initial_turn || !key || !digest) {
      return
    }
    const token = `${key}:${digest}`

    if (seededReview.current === token) {
      return
    }
    seededReview.current = token
    setBusy('review')

    void client
      .sendReview(profile, key, digest, null)
      .then(result => queryClient.setQueryData(reviewKey, result))
      .catch(error => setNotice(errorMessage(error)))
      .finally(() => setBusy(''))
  }, [reviewQuery.data?.needs_initial_turn, key, digest, profile])

  async function refreshActiveRecording() {
    await queryClient.invalidateQueries({ queryKey: activeRecordingKey })
    await queryClient.invalidateQueries({
      queryKey: [client.scope, 'active-recording-status', profile]
    })
  }

  async function startRecording() {
    setBusy('start')
    setNotice('')
    setInstalled(null)

    try {
      const started = await client.startRecording({
        profile,
        name: form.name.trim(),
        instructions: form.instructions.trim(),
        schedule: form.schedule.trim(),
        runMode: form.runMode
      })
      setRecording(started)
      setDraft(null)
      await refreshActiveRecording()
    } catch (error) {
      setNotice(errorMessage(error))
    } finally {
      setBusy('')
    }
  }

  async function startDraft(reviewThreadId = '') {
    if (!recording || !key) {
      return
    }

    setBusy('draft')
    setNotice('')

    try {
      if (recording.status === 'recording') {
        await client.stopRecording(profile, key)
      }
      const operation = await client.startDraft({
        profile,
        key,
        name: form.name.trim() || recording.name,
        instructions: form.instructions.trim() || recording.instructions,
        schedule: form.schedule.trim(),
        expectedDigest: digest,
        reviewThreadId
      })
      processedOperation.current = ''
      setOperationId(operation.operation_id)
      setRecording(current => (current ? { ...current, status: 'stopped' } : current))
    } catch (error) {
      setBusy('')
      setNotice(errorMessage(error))
    }
  }

  async function cancelDraft() {
    if (!operationId) {
      return
    }

    try {
      await client.cancelDraft(operationId)
      await queryClient.invalidateQueries({
        queryKey: [client.scope, 'draft-operation', profile, operationId]
      })
    } catch (error) {
      setNotice(errorMessage(error))
    }
  }

  async function discard() {
    if (!recording || !key) {
      return
    }

    setBusy('discard')
    setNotice('')

    try {
      await client.discardRecording(profile, key)
      setRecording(null)
      setDraft(null)
      setForm({ name: '', instructions: '', schedule: DEFAULT_SCHEDULE, runMode: 'schedule' })
      setReviewMessage('')
      await refreshActiveRecording()
    } catch (error) {
      throw new Error(errorMessage(error))
    } finally {
      setBusy('')
    }
  }

  async function sendReview() {
    const message = reviewMessage.trim()

    if (!message || !key || !digest) {
      return
    }

    setBusy('review')
    setNotice('')

    try {
      const result = await client.sendReview(profile, key, digest, message)
      queryClient.setQueryData(reviewKey, result)
      setReviewMessage('')
    } catch (error) {
      setNotice(errorMessage(error))
    } finally {
      setBusy('')
    }
  }

  async function setJob() {
    if (
      !draft?.recording_dir ||
      !digest ||
      (form.runMode === 'schedule' && !form.schedule.trim())
    ) {
      return
    }

    setBusy('install')
    setNotice('')

    try {
      const result = await client.setHermesJob({
        profile,
        recordingDir: draft.recording_dir,
        schedule: form.schedule.trim(),
        runMode: form.runMode,
        digest
      })
      if (result?.success === false) {
        throw new Error(result.error || t('errors.jobRejected'))
      }
      setInstalled(result)
      await refreshActiveRecording()
    } catch (error) {
      setNotice(errorMessage(error))
    } finally {
      setBusy('')
    }
  }

  function newWorkflow() {
    setInstalled(null)
    setNotice('')
    setForm({ name: '', instructions: '', schedule: DEFAULT_SCHEDULE, runMode: 'schedule' })
    setRecording(null)
    setDraft(null)
    setReviewMessage('')
  }

  const content = installed
    ? element(InstalledJob, { installed, onNew: newWorkflow })
    : draft
      ? element(WorkflowReview, {
          busy,
          draft,
          message: reviewMessage,
          onRedraft: () => void startDraft(reviewQuery.data?.thread_id || ''),
          onSendReview: sendReview,
          onSetJob: setJob,
          review: reviewQuery.data,
          runMode: form.runMode,
          schedule: form.schedule,
          setMessage: setReviewMessage,
          webhookStatus
        })
      : recording
        ? element(ActiveRecording, {
            busy,
            onCancelDraft: cancelDraft,
            onDiscard: () => setDiscardOpen(true),
            onStopAndDraft: startDraft,
            operation: draftQuery.data,
            recording
          })
        : element(WorkflowForm, {
            busy,
            form,
            onStart: startRecording,
            setForm
          })

  return element('div', { className: 'flex h-full min-h-0 flex-col' }, [
    element(
      'header',
      {
        className:
          'flex shrink-0 items-center justify-between gap-3 border-b border-(--ui-stroke-secondary) px-[clamp(1.25rem,4vw,4rem)] py-3',
        key: 'header'
      },
      [
        element('div', { className: 'min-w-0', key: 'copy' }, [
          element(
            'h1',
            { className: 'text-base font-semibold text-(--ui-text-primary)', key: 'title' },
            t('page.title')
          ),
          element(
            'p',
            { className: 'truncate text-xs text-(--ui-text-tertiary)', key: 'description' },
            t('page.description')
          )
        ])
      ]
    ),
    element(
      'main',
      {
        className: 'min-h-0 flex-1 overflow-y-auto px-[clamp(1.25rem,4vw,4rem)] py-4',
        key: 'main'
      },
      activeRecordingQuery.isLoading
        ? element(
            'div',
            { className: 'flex h-full min-h-64 items-center justify-center' },
            element(Loader, { label: t('page.loading'), type: 'lemniscate-bloom' })
          )
        : element(
            'div',
            {
              className: 'mx-auto flex w-full max-w-4xl min-w-0 flex-col gap-3'
            },
            [
              activeRecordingQuery.error
                ? element(Notice, { key: 'load-error', kind: 'error' }, errorMessage(activeRecordingQuery.error))
                : null,
              notice ? element(Notice, { key: 'notice', kind: 'error' }, notice) : null,
              draftQuery.error
                ? element(Notice, { key: 'draft-error', kind: 'error' }, errorMessage(draftQuery.error))
                : null,
              reviewQuery.error && draft
                ? element(Notice, { key: 'review-error', kind: 'error' }, errorMessage(reviewQuery.error))
                : null,
              content
            ]
          )
    ),
    element(ConfirmDialog, {
      busyLabel: t('recording.discarding'),
      confirmLabel: t('recording.discardConfirm'),
      destructive: true,
      description: t('recording.discardDescription'),
      key: 'discard-dialog',
      onClose: () => setDiscardOpen(false),
      onConfirm: discard,
      open: discardOpen,
      title: t('recording.discardTitle')
    })
  ])
}

function RecordPage({ client }) {
  const profile = useValue(host.state.profile)

  return jsx(RecordProfilePage, { client, profile }, profile)
}

function RecordStatus({ client }) {
  const t = usePluginI18n(ID)
  const profile = useValue(host.state.profile)
  const { data, error } = useQuery({
    queryKey: [client.scope, 'active-recording-status', profile],
    queryFn: () => client.activeRecording(profile),
    refetchInterval: query =>
      query.state.data?.recording?.status === 'recording' ? ACTIVE_POLL_MS : false,
    retry: false
  })
  const recording = data?.recording
  const active = recording?.status === 'recording'

  return element(
    'button',
    {
      className:
        'inline-flex h-full items-center gap-1.5 px-1.5 text-[0.6875rem] text-(--ui-text-tertiary) transition-colors hover:bg-(--ui-control-hover-background) hover:text-(--ui-text-primary)',
      'aria-label': t('status.open'),
      onClick: () => host.navigate(ROUTE),
      type: 'button'
    },
    [
      element(StatusDot, {
        key: 'dot',
        tone: error ? 'bad' : active ? 'good' : 'muted'
      }),
      active
        ? t('status.recording')
        : error
          ? t('status.unavailable')
          : t('status.label')
    ]
  )
}

export default {
  id: ID,
  name: 'Record',
  register(ctx) {
    ctx.i18n.register({
      en: {
        page: {
          title: 'Workflows',
          description:
            'Describe the result and how it starts, then show Hermes the work once.',
          loading: 'Loading workflows'
        },
        status: {
          label: 'workflows',
          recording: 'recording',
          review: 'review workflow',
          unavailable: 'workflows unavailable',
          open: 'Open Record workflows'
        },
        new: {
          title: '1. Define and record',
          description: 'Give Hermes the intent it needs to draft the workflow, then perform the task normally.',
          name: 'Workflow name',
          namePlaceholder: 'Daily vendor status brief',
          instructions: 'What should Hermes reproduce?',
          instructionsHint: 'State the intended result and any limits Hermes must keep.',
          instructionsPlaceholder: 'Open the vendor portal, collect the latest status, and prepare the same brief…',
          schedule: 'Schedule',
          scheduleHint: 'Choose when Hermes should run this workflow.',
          start: 'Start recording',
          starting: 'Preparing recorder…'
        },
        trigger: {
          label: 'How should this workflow start?',
          schedule: 'On a schedule',
          webhook: 'When an event happens',
          eventHint: 'Include the exact event in the result above. Hermes will identify the source, filters, and support while drafting.',
          webhookTitle: 'Event start',
          webhookDescription: 'Hermes will verify the drafted event before this workflow can be turned on.',
          detected: 'Event supported',
          needsDefinition: 'Event needs review',
          missingDefinition: 'Ask Hermes to identify the event and update the draft before turning on this workflow.',
          events: value => `Expected event types: ${value}`,
          receiverReady: 'This profile’s signed webhook receiver is ready.',
          receiverNotReady: 'Configure and start this profile’s webhook receiver before activation.'
        },
        recording: {
          activeTitle: 'Recording your workflow',
          activeDescription: 'Do the task once as you normally would, then return here.',
          savedTitle: 'Recording saved',
          savedDescription: 'The capture is sealed and ready for Hermes to draft.',
          drafting: 'Hermes is drafting the workflow…',
          discard: 'Discard recording',
          discardTitle: 'Discard this recording?',
          discardDescription: 'This removes the captured workflow. You cannot reopen it from Workflows.',
          discardConfirm: 'Discard recording',
          discarding: 'Discarding…',
          cancelDraft: 'Cancel drafting',
          stopAndDraft: 'Stop and draft',
          retryDraft: 'Retry draft'
        },
        review: {
          title: 'Review your workflow',
          description: 'Check what Hermes will do and resolve any unclear detail before turning it on.',
          ready: 'ready',
          checking: 'checking',
          states: {
            needs_input: 'needs input',
            needs_rerecord: 'record again',
            ready_to_redraft: 'ready to update',
            ready_to_schedule: 'ready'
          },
          steps: 'Execution steps',
          noSteps: 'Hermes did not return a readable step list. Ask Hermes to update the draft.',
          needsAttention: 'Needs attention',
          starts: 'Starts',
          conversation: 'Ask Hermes a question or request a change',
          messageLabel: 'Message for Hermes',
          noMessages: 'No unresolved details. Ask Hermes a question or request a change.',
          loading: 'Loading the workflow review…',
          messagePlaceholder: 'Ask a question or request a precise change…',
          send: 'Send',
          sending: 'Sending…',
          update: 'Update draft',
          updating: 'Updating…',
          turnOn: 'Turn on workflow',
          turningOn: 'Turning on workflow…',
        },
        installed: {
          title: 'Workflow is on',
          description: 'Hermes Cron owns the schedule, execution sessions, and run history.',
          webhookJobDescription: 'Hermes Cron owns the durable job, queued events, execution sessions, and run history.',
          active: 'active',
          awaitingEvent: 'awaiting event',
          jobId: 'Job ID',
          webhookReady: 'Webhook endpoint ready',
          webhookDescription: 'Send signed event payloads to this URL. Matching events queue the reviewed job; non-matching or invalid requests are rejected.',
          copyUrl: 'Copy webhook URL',
          signingSecret: 'Show signing secret',
          secretWarning: 'Store this secret in the sending system. Hermes uses it to verify each request.',
          copySecret: 'Copy signing secret',
          new: 'Record another workflow',
          manage: 'Manage in Cron'
        },
        errors: {
          nameRequired: 'Enter a workflow name.',
          instructionsRequired: 'Describe what Hermes should reproduce.',
          scheduleRequired: 'Enter a Cron schedule.',
          draftCanceled: 'Workflow drafting was canceled.',
          draftFailed: 'Workflow drafting failed. Retry when the recorder and model are available.',
          jobRejected: 'Hermes Cron rejected this job.'
        }
      }
    })

    const client = createClient(ctx)

    ctx.registerMany([
      {
        id: 'page',
        area: ROUTES_AREA,
        data: { path: ROUTE },
        render: () => jsx(RecordPage, { client })
      },
      {
        id: 'nav',
        area: SIDEBAR_NAV_AREA,
        data: { path: ROUTE, label: ctx.i18n.t('page.title'), codicon: 'record-keys' }
      },
      {
        id: 'open',
        area: PALETTE_AREA,
        data: {
          id: 'record.open',
          label: ctx.i18n.t('status.open'),
          keywords: ['record', 'workflow', 'record', 'cron'],
          run: () => host.navigate(ROUTE)
        }
      },
      {
        id: 'status',
        area: STATUSBAR_AREAS.right,
        order: 118,
        render: () => jsx(RecordStatus, { client })
      }
    ])
  }
}
