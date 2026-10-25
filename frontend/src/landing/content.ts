/**
 * Copy and data for the public landing page. Kept in one place so the section
 * components stay about layout, and so the honest-claims rules (no "certified",
 * no testimonials, "Price on request") are easy to audit.
 */
import type { PendingPromptKind } from '../lib/pendingPrompt'

/* ───────────── hero composer ───────────── */

export interface Suggestion {
  text: string
  kind: PendingPromptKind
}

export const SUGGESTIONS: Suggestion[] = [
  { text: 'Biosimilar PK study of adalimumab in healthy volunteers', kind: 'protocol' },
  { text: 'Bioequivalence study for metformin 850 mg tablets', kind: 'protocol' },
  { text: 'Price a patient study for nivolumab', kind: 'budget' },
]

/* ───────────── pinned story: a study being built ───────────── */

/** The study the story builds. Same wording as the first hero suggestion. */
export const STORY_PROMPT = 'Biosimilar PK study of adalimumab in healthy volunteers'

/** Shown once in the story frame. Every number in the story is an example. */
export const STORY_NOTE = 'Illustration — example values'

export interface StoryStage {
  n: number
  title: string
  body: string
}

export const STORY_STAGES: StoryStage[] = [
  {
    n: 1,
    title: 'Describe the study.',
    body: 'Say it the way you would to a colleague. QuarkClinical looks up the drug and asks only for what is missing.',
  },
  {
    n: 2,
    title: 'Confirm the design.',
    body: 'The drug match and study type come back as cards. Adalimumab is a biologic, so it is set up as a biosimilar PK study; bioequivalence and food effect do not apply. Nothing is written until you confirm.',
  },
  {
    n: 3,
    title: 'Engines do the math.',
    body: 'Sample size, the PK sampling schedule and the design decisions are computed by tested engines and locked once, each with its reasoning.',
  },
  {
    n: 4,
    title: 'The protocol writes itself around it.',
    body: 'All 17 sections are drafted around the locked numbers, so the synopsis, design and statistics can never disagree.',
  },
  {
    n: 5,
    title: 'Priced from its own trial history.',
    body: 'Enrollment, sites and countries come from the drug’s registered ClinicalTrials.gov trials. Every line shows its quantity basis and where its rate came from.',
  },
]

/** The 17 sections of a generated protocol, in order (backend SECTION_DISPLAY_NAMES). */
export const PROTOCOL_SECTIONS: { title: string; locked?: boolean }[] = [
  { title: 'Title Page' },
  { title: 'Synopsis', locked: true },
  { title: 'Introduction' },
  { title: 'Study Rationale & Risk/Benefit' },
  { title: 'Study Objectives' },
  { title: 'Study Endpoints' },
  { title: 'Study Design', locked: true },
  { title: 'Population' },
  { title: 'Treatment' },
  { title: 'Study Assessments' },
  { title: 'Safety Monitoring' },
  { title: 'Data Handling & Record Keeping' },
  { title: 'Study Monitoring, Auditing & Inspecting' },
  { title: 'Statistical Methods & Data Analysis', locked: true },
  { title: 'Ethical & Regulatory Standards' },
  { title: 'Administrative Procedures' },
  { title: 'References' },
]

/** Example budget lines for the last stage. They sum to BUDGET_TOTAL. */
export const BUDGET_LINES: { label: string; amount: number }[] = [
  { label: 'Clinical unit and site fees', amount: 520000 },
  { label: 'Bioanalysis', amount: 310000 },
  { label: 'Data management and biostatistics', amount: 240000 },
  { label: 'Project management and monitoring', amount: 200000 },
  { label: 'Regulatory, ethics and start-up', amount: 150000 },
]
export const BUDGET_TOTAL = BUDGET_LINES.reduce((sum, l) => sum + l.amount, 0)

/* ───────────── metrics band (true facts only) ───────────── */

export const METRICS: { value: number; label: string; note: string }[] = [
  {
    value: 17,
    label: 'protocol sections',
    note: 'From the title page to the references, drafted around one set of locked numbers.',
  },
  {
    value: 4,
    label: 'guideline sets',
    note: 'FDA bioequivalence 2014, EMA bioequivalence 2010, ICH M13A 2024 and the ICH M11 2025 template.',
  },
  {
    value: 8,
    label: 'deterministic engines',
    note: 'Sampling schedule, drug interactions, safety gating, bioanalytical validation, ICF, randomization, CRF and data management, and IND safety reporting.',
  },
  {
    value: 3,
    label: 'tools, one study',
    note: 'Protocol Generator, Budgeting Tool and Study Reports share the same study and the same numbers.',
  },
]

/* ───────────── guidance ───────────── */

export const GUIDANCE = [
  'FDA bioequivalence guidance',
  'FDA biosimilar PK similarity (351(k))',
  'EMA investigation of bioequivalence',
  'EMA similar biological medicinal products',
  'ICH M13A',
  'ICH M11 protocol template',
  'ICH E6 GCP',
  'ICH E9(R1)',
]

export const SOURCES = ['PubMed', 'DailyMed labels', 'ClinicalTrials.gov']

/* ───────────── products ───────────── */

export type ProductKey = 'protocol' | 'budget' | 'report'

export const PRODUCTS: {
  key: ProductKey
  name: string
  tagline: string
  points: string[]
}[] = [
  {
    key: 'protocol',
    name: 'Protocol Generator',
    tagline: 'A reviewable protocol draft, section by section.',
    points: [
      'Bioequivalence, food-effect and biosimilar PK study designs',
      'Exact TOST sample size (ABE, RSABE, NTI) locked across every section',
      'Sampling schedule, safety gating, randomization, ICF and CRF plans computed, not written',
      'DOCX and PDF export with versions and page-numbered amendments',
    ],
  },
  {
    key: 'budget',
    name: 'Budgeting Tool',
    tagline: 'A priced study budget from a drug name.',
    points: [
      'Every line priced with its quantity basis and source',
      'Enrollment, sites and countries grounded in the drug’s own ClinicalTrials.gov history',
      'Healthy-volunteer and patient study profiles',
      'Link a protocol to price its real schedule; export to Word',
    ],
  },
  {
    key: 'report',
    name: 'Study Reports',
    tagline: 'From study data to a clinical study report draft.',
    points: [
      'Upload real PK data from the completed study',
      'Non-compartmental analysis and bioequivalence statistics',
      'ICH E3 narrative written around locked results',
      'DOCX and PDF export',
    ],
  },
]

/* ───────────── why ───────────── */

export type WhyIconKey = 'chart' | 'beaker' | 'stethoscope' | 'lock' | 'users' | 'shield'

export const WHY: { icon: WhyIconKey; title: string; body: string }[] = [
  {
    icon: 'chart',
    title: 'Exact sample size',
    body: 'Two one-sided tests on the non-central t distribution, checked against published PowerTOST reference tables. Handles average BE, reference-scaled BE for highly variable drugs, and narrow therapeutic index limits.',
  },
  {
    icon: 'beaker',
    title: 'Sourced PK, never invented',
    body: 'PK parameters come from PubMed abstracts and DailyMed labels with their PMIDs. When the literature has no value, the assumption is flagged, not filled in.',
  },
  {
    icon: 'stethoscope',
    title: 'Clinical design rules built in',
    body: 'Developed with a clinical pharmacologist: healthy volunteers versus patients by safety profile, multiple-dose steady-state PK for target-mediated drugs, and the lowest-variability indication for patient studies.',
  },
  {
    icon: 'lock',
    title: 'One set of numbers',
    body: 'The sample size and design are computed once and locked, so the synopsis, design and statistical sections can never disagree.',
  },
  {
    icon: 'users',
    title: 'Review, sign, amend',
    body: 'Author, reviewer and approver roles, electronic signature, version history and an audit trail on every change.',
  },
  {
    icon: 'shield',
    title: 'Built to be checked',
    body: 'Every design decision carries its rationale and evidence, and every section shows its sources, so a reviewer can verify rather than trust.',
  },
]

/* ───────────── how it works ───────────── */

export const STEPS = [
  {
    n: '01',
    title: 'Describe the study',
    body: 'Tell QuarkClinical the drug and the study in plain language. It looks up the drug, its label and prior-trial data, and asks only for what is missing.',
  },
  {
    n: '02',
    title: 'Confirm the design',
    body: 'Population, dose, route, design and endpoints come back as cards with their reasoning. Accept or change each one before anything is written.',
  },
  {
    n: '03',
    title: 'Generate and price',
    body: 'The protocol is drafted around the computed numbers, and the budget is priced from the same study.',
  },
  {
    n: '04',
    title: 'Review, sign and export',
    body: 'Edit any section, route it through review and approval, and export DOCX or PDF.',
  },
]

/* ───────────── pricing ───────────── */

export interface Plan {
  name: string
  blurb: string
  features: string[]
  cta: string
  /** Pre-selected interest chip in the contact modal. */
  interest: string
  featured: boolean
}

export const PLANS: Plan[] = [
  {
    name: 'Pilot',
    blurb: 'Prove it on one real study.',
    features: [
      'One study, end to end: protocol and budget',
      'Guided onboarding and a review of the outputs with our team',
      'Fixed scope agreed up front',
      'Move to a plan when you are ready',
    ],
    cta: 'Request a pilot',
    interest: 'Request a pilot on a real study',
    featured: false,
  },
  {
    name: 'Team',
    blurb: 'For sponsors and consultancies running several studies.',
    features: [
      'Protocol Generator and Budgeting Tool',
      'Seats for your team with author, reviewer and approver roles',
      'A monthly usage allowance included',
      'DOCX, PDF and Word exports',
      'Email support',
    ],
    cta: 'Contact us',
    interest: 'Pricing and plans',
    featured: true,
  },
  {
    name: 'Enterprise',
    blurb: 'For larger programs and CROs.',
    features: [
      'All tools, including Study Reports',
      'Higher allowances and annual terms',
      'Priority onboarding and support',
      'Help with security reviews and questionnaires',
    ],
    cta: 'Contact us',
    interest: 'Pricing and plans',
    featured: false,
  },
]

/* ───────────── FAQ ───────────── */

export const FAQS: { q: string; a: string }[] = [
  {
    q: 'Is the output ready to submit to a regulator?',
    a: 'No. QuarkClinical produces drafts for investigational use. Every protocol, budget and report should be reviewed and approved by qualified experts on your team before any regulatory or contractual use. The review, approval and e-signature workflow is built in for that purpose.',
  },
  {
    q: 'Does it make up PK values?',
    a: 'No. PK parameters are extracted from PubMed abstracts and DailyMed labels and tagged with their source. If the literature has no value, the within-subject CV falls back to a clearly flagged assumption, and you can enter your own.',
  },
  {
    q: 'Does the assistant make up numbers?',
    a: 'No. It collects your inputs and explains; sample sizes, schedules, design decisions and prices come from deterministic engines and appear in cards you confirm.',
  },
  {
    q: 'How is the sample size calculated?',
    a: 'With an exact two one-sided tests (TOST) engine using the non-central t distribution, checked against published PowerTOST reference tables. It supports average bioequivalence, reference-scaled bioequivalence for highly variable drugs, and tightened limits for narrow therapeutic index drugs.',
  },
  {
    q: 'Which studies are supported?',
    a: 'Small-molecule bioequivalence and food-effect studies, and biosimilar PK similarity studies in healthy volunteers or patients. For patient studies, QuarkClinical proposes the indication with the lowest expected PK variability and a multiple-dose steady-state design where the drug shows target-mediated disposition, and you can override both.',
  },
  {
    q: 'How is a budget priced?',
    a: 'From a documented rate card, with each line showing its quantity basis and source. Enrollment, site and country counts come from the drug’s own registered trials on ClinicalTrials.gov when they exist, or from your linked protocol or your own inputs. Budgets are estimates to compare against CRO quotes, not quotes themselves.',
  },
  {
    q: 'What happens to our study data?',
    a: 'Each organization has its own workspace and access is controlled by role. Study content is processed by Anthropic’s Claude models to draft text, as listed in our Privacy Policy, and is not used to train models.',
  },
  {
    q: 'Where are chat conversations stored?',
    a: 'In your organization’s workspace. A conversation is visible only to the person who started it; the protocols and budgets it produces are shared with your organization as usual.',
  },
  {
    q: 'Can we try it before committing?',
    a: 'Yes. A pilot runs QuarkClinical on one of your real studies with our team, with the scope agreed up front.',
  },
]
