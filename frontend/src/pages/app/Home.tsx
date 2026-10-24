import { useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import {
  Badge,
  Banner,
  Chip,
  ChevronRight,
  Composer,
  FileText,
  MessageSquare,
  PillNav,
  Sparkles,
  ToolTile,
} from '../../ui'
import type { BadgeVariant } from '../../ui'
import { useActingUser } from '../../lib/actingUser'
import { useOrg } from '../../lib/org'
import { CHAT_KIND_LABELS } from '../../shell/chatList'
import { NEW_CHAT_PATH, newStudyHref } from '../../shell/config'
import type { StudyKind } from '../../shell/config'
import { capitalize, displayNameFrom, greetingFor, relativeTime, STUDY_TYPE_LABELS } from '../../shell/format'
import { saveHomeDraft } from '../../shell/homeDraft'
import {
  BiosimilarIllustration,
  BioequivalenceIllustration,
  BudgetIllustration,
  EditProtocolIllustration,
  FoodEffectIllustration,
  StudyReportIllustration,
} from '../../shell/illustrations'
import { useShell } from '../../shell/ShellContext'
import type { ProtocolRow } from '../../shell/ShellContext'

interface TileDef {
  id: string
  label: string
  to: string
  illustration: ReactNode
}

const MODE_ITEMS = [
  { id: 'protocol', label: 'Protocol', icon: <FileText /> },
  { id: 'budget', label: 'Budget', icon: <Sparkles /> },
]

const SUGGESTIONS: { text: string; kind: StudyKind }[] = [
  { text: 'Biosimilar PK study of adalimumab in healthy volunteers', kind: 'protocol' },
  { text: 'Bioequivalence study for metformin 850 mg tablets', kind: 'protocol' },
  { text: 'Price a patient study for nivolumab', kind: 'budget' },
]

const STATUS_BADGE: Record<string, { label: string; variant: BadgeVariant }> = {
  draft: { label: 'Draft', variant: 'neutral' },
  review: { label: 'In review', variant: 'warning' },
  approved: { label: 'Approved', variant: 'success' },
}

function statusBadge(p: ProtocolRow): { label: string; variant: BadgeVariant } {
  if (p.generation_status === 'generating') return { label: 'Generating', variant: 'info' }
  if (p.generation_status === 'failed') return { label: 'Failed', variant: 'danger' }
  return STATUS_BADGE[p.status] ?? { label: capitalize(p.status), variant: 'neutral' }
}

function SectionHeader({ id, title, to, linkLabel }: { id: string; title: string; to?: string; linkLabel?: string }) {
  return (
    <div className="mb-3 flex items-baseline justify-between gap-3">
      <h2 id={id} className="text-h2 text-fg">{title}</h2>
      {to ? (
        <Link to={to} className="inline-flex items-center gap-0.5 rounded-md text-small font-medium text-fg-muted no-underline hover:text-fg">
          {linkLabel ?? 'View all'}
          <ChevronRight aria-hidden className="h-3.5 w-3.5" />
        </Link>
      ) : null}
    </div>
  )
}

function RowLink({ to, icon, title, subtitle, trailing }: { to: string; icon: ReactNode; title: string; subtitle: string; trailing?: ReactNode }) {
  return (
    <Link
      to={to}
      className="flex items-center gap-3 rounded-2xl p-2 text-fg no-underline transition-colors duration-fast hover:bg-surface-hover"
    >
      <span
        aria-hidden
        className="flex h-11 w-11 flex-none items-center justify-center rounded-xl bg-surface-muted text-fg-muted [&>svg]:h-5 [&>svg]:w-5"
      >
        {icon}
      </span>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-body font-medium">{title}</span>
        <span className="block truncate text-small text-fg-muted">{subtitle}</span>
      </span>
      {trailing}
    </Link>
  )
}

function ListSkeleton() {
  return (
    <div className="space-y-2" aria-hidden>
      {[0, 1, 2].map(i => (
        <div key={i} className="flex items-center gap-3 p-2">
          <div className="h-11 w-11 animate-pulse rounded-xl bg-surface-muted" />
          <div className="flex-1 space-y-2">
            <div className="h-3.5 w-2/5 animate-pulse rounded-full bg-surface-muted" />
            <div className="h-3 w-3/5 animate-pulse rounded-full bg-surface-muted" />
          </div>
        </div>
      ))}
    </div>
  )
}

function LatestProtocols() {
  const { protocols, protocolsError } = useShell()
  const latest = useMemo(() => (protocols ?? []).slice(0, 5), [protocols])

  return (
    <section aria-labelledby="home-latest-protocols">
      <SectionHeader id="home-latest-protocols" title="Latest protocols" to="/app/protocols" />
      {protocols === null && !protocolsError ? <ListSkeleton /> : null}
      {protocolsError && protocols === null ? (
        <Banner tone="danger" title="Could not load your protocols">
          {protocolsError}
        </Banner>
      ) : null}
      {protocols && latest.length === 0 ? (
        <p className="rounded-2xl bg-surface-muted px-4 py-6 text-body text-fg-muted">
          No protocols yet. Start a study above and it will show up here.
        </p>
      ) : null}
      {latest.length > 0 ? (
        <ul className="-mx-2 flex flex-col">
          {latest.map(p => {
            const badge = statusBadge(p)
            return (
              <li key={p.id}>
                <RowLink
                  to={`/app/protocols/${p.id}`}
                  icon={<FileText />}
                  title={`${capitalize(p.drug_inn)} · ${STUDY_TYPE_LABELS[p.study_type] ?? p.study_type}`}
                  subtitle={`v${p.version} · ${p.guideline_version} · ${relativeTime(p.created_at)}`}
                  trailing={<Badge variant={badge.variant}>{badge.label}</Badge>}
                />
              </li>
            )
          })}
        </ul>
      ) : null}
    </section>
  )
}

function Conversations() {
  const { recentChats } = useShell()
  const items = recentChats?.available ? recentChats.items.slice(0, 5) : []

  return (
    <section aria-labelledby="home-conversations">
      <SectionHeader id="home-conversations" title="Continue a conversation" />
      {recentChats === null ? <ListSkeleton /> : null}
      {recentChats && items.length === 0 ? (
        <p className="rounded-2xl bg-surface-muted px-4 py-6 text-body text-fg-muted">
          {recentChats.available
            ? 'No conversations yet. Describe a study above to start one.'
            : 'Conversations about your studies will appear here.'}
        </p>
      ) : null}
      {items.length > 0 ? (
        <ul className="-mx-2 flex flex-col">
          {items.map(chat => (
            <li key={chat.id}>
              <RowLink
                to={`/app/chat/${chat.id}`}
                icon={<MessageSquare />}
                title={chat.title?.trim() || 'Untitled conversation'}
                subtitle={`${CHAT_KIND_LABELS[chat.kind] ?? 'Chat'} · ${relativeTime(chat.last_activity_at)}`}
              />
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  )
}

export default function Home() {
  const navigate = useNavigate()
  const { org } = useOrg()
  const { currentUser } = useActingUser()
  const [text, setText] = useState('')
  const [kind, setKind] = useState<StudyKind>('protocol')

  const clerkFirstName = (window as unknown as { Clerk?: { user?: { firstName?: string | null } } }).Clerk?.user?.firstName
  const name = displayNameFrom(clerkFirstName, currentUser?.email)
  const greeting = greetingFor(new Date().getHours())

  const tiles: TileDef[] = [
    { id: 'biosimilar', label: 'Biosimilar PK protocol', to: newStudyHref('protocol', 'biosimilar_pk'), illustration: <BiosimilarIllustration /> },
    { id: 'be', label: 'Bioequivalence protocol', to: newStudyHref('protocol', 'be'), illustration: <BioequivalenceIllustration /> },
    { id: 'food', label: 'Food-effect protocol', to: newStudyHref('protocol', 'food_effect'), illustration: <FoodEffectIllustration /> },
    { id: 'budget', label: 'Study budget', to: newStudyHref('budget'), illustration: <BudgetIllustration /> },
    { id: 'report', label: 'Study report', to: '/app/reports', illustration: <StudyReportIllustration /> },
    { id: 'edit', label: 'Edit a protocol', to: '/app/protocols', illustration: <EditProtocolIllustration /> },
  ]

  // Chat sessions are not live yet: park the text (sessionStorage `qc_home_draft`,
  // see shell/homeDraft.ts) and open the new-chat page, which picks it up.
  const send = (value: string) => {
    saveHomeDraft({ text: value, kind })
    navigate(`${NEW_CHAT_PATH}?kind=${kind}&from=home`)
  }

  return (
    <div className="mx-auto w-full max-w-5xl px-4 pb-16 pt-6 sm:px-8 sm:pt-8">
      <Link
        to={NEW_CHAT_PATH}
        className="group inline-flex max-w-full items-center gap-2 rounded-pill border border-line bg-surface p-1 pr-3 text-small text-fg no-underline shadow-xs transition-colors duration-fast hover:bg-surface-hover"
      >
        <Badge variant="solid">New</Badge>
        <span className="truncate font-medium">Set up a study by chat</span>
        <ChevronRight aria-hidden className="h-3.5 w-3.5 flex-none text-fg-subtle transition-transform duration-fast group-hover:translate-x-0.5" />
      </Link>

      <div className="mt-8">
        <p className="text-body text-fg-subtle">{org?.name ?? 'My workspace'}</p>
        <h1 className="mt-1 text-h1 text-fg">
          {greeting}
          {name ? `, ${name}` : ''}
        </h1>
      </div>

      <div className="mt-6 max-w-3xl">
        <Composer
          value={text}
          onChange={setText}
          onSend={send}
          placeholder="Describe your study, for example a biosimilar PK study of adalimumab in healthy volunteers"
          textareaLabel="Describe your study"
          sendLabel="Start study"
          minRows={2}
          maxLength={2000}
          footerLeft={
            <PillNav
              ariaLabel="What are you setting up?"
              size="sm"
              items={MODE_ITEMS}
              value={kind}
              onChange={id => setKind(id as StudyKind)}
            />
          }
        />
        <div className="mt-3 flex flex-wrap gap-2" aria-label="Suggestions" role="group">
          {SUGGESTIONS.map(s => (
            <Chip
              key={s.text}
              icon={<Sparkles />}
              onClick={() => {
                setText(s.text)
                setKind(s.kind)
              }}
              className="max-w-full"
            >
              <span className="truncate">{s.text}</span>
            </Chip>
          ))}
        </div>
      </div>

      <nav aria-label="Start something" className="mt-10">
        <ul className="grid grid-cols-2 gap-x-4 gap-y-6 sm:grid-cols-3 min-[1280px]:grid-cols-6">
          {tiles.map(tile => (
            <li key={tile.id} className="min-w-0">
              <ToolTile as={Link} to={tile.to} label={tile.label} illustration={tile.illustration} />
            </li>
          ))}
        </ul>
      </nav>

      <div className="mt-14 grid gap-10 lg:grid-cols-2">
        <LatestProtocols />
        <Conversations />
      </div>
    </div>
  )
}
