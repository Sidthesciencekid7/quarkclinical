import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import {
  BreadcrumbProvider,
  crumbKey,
  crumbsForPath,
  useBreadcrumbs,
  useSetCrumb,
} from '../../lib/breadcrumbs'

describe('crumbsForPath', () => {
  it('Home for the app index, with or without a trailing slash', () => {
    expect(crumbsForPath('/app')).toEqual([{ label: 'Home' }])
    expect(crumbsForPath('/app/')).toEqual([{ label: 'Home' }])
  })

  it('returns nothing outside the app', () => {
    expect(crumbsForPath('/')).toEqual([])
    expect(crumbsForPath('/admin')).toEqual([])
  })

  it('single-level sections have no links', () => {
    expect(crumbsForPath('/app/protocols')).toEqual([{ label: 'Protocols' }])
    expect(crumbsForPath('/app/budgets')).toEqual([{ label: 'Budgets' }])
    expect(crumbsForPath('/app/reports')).toEqual([{ label: 'Study reports' }])
    expect(crumbsForPath('/app/team')).toEqual([{ label: 'Team' }])
    expect(crumbsForPath('/app/chat/new')).toEqual([{ label: 'New study' }])
  })

  it('a protocol shows a neutral label until a page publishes one, never the raw id', () => {
    const id = '72740aaf-b82e-4f65-9ff7-04b01fa3515a'
    expect(crumbsForPath(`/app/protocols/${id}`)).toEqual([
      { label: 'Protocols', to: '/app/protocols' },
      { label: 'Protocol' },
    ])
    expect(crumbsForPath(`/app/protocols/${id}`, { [crumbKey.protocol(id)]: 'Adalimumab · Biosimilar PK' })).toEqual([
      { label: 'Protocols', to: '/app/protocols' },
      { label: 'Adalimumab · Biosimilar PK' },
    ])
  })

  it('the study report page links back to its protocol', () => {
    const labels = { [crumbKey.protocol('p1')]: 'Ibuprofen · Bioequivalence' }
    expect(crumbsForPath('/app/protocols/p1/report', labels)).toEqual([
      { label: 'Protocols', to: '/app/protocols' },
      { label: 'Ibuprofen · Bioequivalence', to: '/app/protocols/p1' },
      { label: 'Study report' },
    ])
  })

  it('budgets and chat sessions use their own label namespaces', () => {
    expect(crumbsForPath('/app/budgets/b1', { [crumbKey.budget('b1')]: 'Trastuzumab study budget' })).toEqual([
      { label: 'Budgets', to: '/app/budgets' },
      { label: 'Trastuzumab study budget' },
    ])
    // A protocol label with the same id must not leak into a budget crumb.
    expect(crumbsForPath('/app/budgets/x', { [crumbKey.protocol('x')]: 'Nope' })[1]).toEqual({ label: 'Budget' })
    expect(crumbsForPath('/app/chat/s1')).toEqual([
      { label: 'New study', to: '/app/chat/new' },
      { label: 'Conversation' },
    ])
  })

  it('edit and budget conversations sit under their own section once the kind is published', () => {
    const labels = { [crumbKey.chat('e1')]: 'Edit adalimumab', [crumbKey.chatKind('e1')]: 'protocol_edit' }
    expect(crumbsForPath('/app/chat/e1', labels)).toEqual([
      { label: 'Protocols', to: '/app/protocols' },
      { label: 'Edit adalimumab' },
    ])
    expect(crumbsForPath('/app/chat/b1', { [crumbKey.chatKind('b1')]: 'budget' })[0]).toEqual({ label: 'Budgets', to: '/app/budgets' })
    expect(crumbsForPath('/app/chat/p1', { [crumbKey.chatKind('p1')]: 'protocol' })[0]).toEqual({ label: 'New study', to: '/app/chat/new' })
  })

  it('the classic wizards sit under New study', () => {
    expect(crumbsForPath('/app/protocol')).toEqual([{ label: 'New study', to: '/app/chat/new' }, { label: 'Protocol wizard' }])
    expect(crumbsForPath('/app/budgeting')).toEqual([{ label: 'New study', to: '/app/chat/new' }, { label: 'Budget wizard' }])
  })
})

function Trail() {
  const crumbs = useBreadcrumbs()
  return (
    <ol>
      {crumbs.map(c => (
        <li key={c.label}>{c.label}</li>
      ))}
    </ol>
  )
}

function Publisher({ id, label }: { id: string; label: string | null }) {
  useSetCrumb(crumbKey.protocol(id), label)
  return null
}

describe('useBreadcrumbs / useSetCrumb', () => {
  it('shows the URL-derived trail and picks up a published label', () => {
    const { rerender } = render(
      <MemoryRouter initialEntries={['/app/protocols/p9']}>
        <BreadcrumbProvider>
          <Trail />
          <Publisher id="p9" label={null} />
        </BreadcrumbProvider>
      </MemoryRouter>,
    )
    expect(screen.getByText('Protocol')).toBeInTheDocument()

    rerender(
      <MemoryRouter initialEntries={['/app/protocols/p9']}>
        <BreadcrumbProvider>
          <Trail />
          <Publisher id="p9" label="Abatacept · Biosimilar PK" />
        </BreadcrumbProvider>
      </MemoryRouter>,
    )
    expect(screen.getByText('Abatacept · Biosimilar PK')).toBeInTheDocument()
  })

  it('drops the label when the publishing page unmounts', () => {
    const { rerender } = render(
      <MemoryRouter initialEntries={['/app/protocols/p9']}>
        <BreadcrumbProvider>
          <Trail />
          <Publisher id="p9" label="Abatacept · Biosimilar PK" />
        </BreadcrumbProvider>
      </MemoryRouter>,
    )
    expect(screen.getByText('Abatacept · Biosimilar PK')).toBeInTheDocument()

    rerender(
      <MemoryRouter initialEntries={['/app/protocols/p9']}>
        <BreadcrumbProvider>
          <Trail />
        </BreadcrumbProvider>
      </MemoryRouter>,
    )
    expect(screen.queryByText('Abatacept · Biosimilar PK')).not.toBeInTheDocument()
    expect(screen.getByText('Protocol')).toBeInTheDocument()
  })
})
