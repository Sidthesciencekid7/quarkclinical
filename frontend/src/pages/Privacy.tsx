import LegalLayout from './LegalLayout'

export default function Privacy() {
  return (
    <LegalLayout title="Privacy Policy" updated="July 12, 2026">
      <p>
        This Privacy Policy describes what information Quark AI (&ldquo;we&rdquo;, &ldquo;us&rdquo;) collects
        when you use the Service, how it is used, and the choices available to you.
      </p>

      <h2>1. Information We Collect</h2>
      <ul>
        <li>
          <strong>Account information</strong> &mdash; name and email address, collected via our authentication
          provider (Clerk) when you sign up or sign in.
        </li>
        <li>
          <strong>Organization data</strong> &mdash; organization name, seat/license status, and team membership,
          used to enforce per-organization data isolation and role-based access.
        </li>
        <li>
          <strong>Protocol content</strong> &mdash; drug names, study parameters, and the resulting generated
          protocol text, PK data, and exports you create using the Service.
        </li>
        <li>
          <strong>Sales inquiries</strong> &mdash; company name, contact name, email, and any message you submit
          through the &ldquo;Contact sales&rdquo; form, used solely to follow up on your enterprise license
          inquiry.
        </li>
        <li>
          <strong>Usage and audit data</strong> &mdash; a record of actions taken on protocols (create, edit,
          sign, export) for audit-trail and security purposes.
        </li>
      </ul>

      <h2>2. How We Use Information</h2>
      <ul>
        <li>To provide, maintain, and secure the Service, including enforcing role-based access and organization isolation;</li>
        <li>To generate protocol drafts and sample-size calculations on your behalf;</li>
        <li>To respond to sales inquiries and activate or manage your organization&rsquo;s license;</li>
        <li>To maintain an audit trail of actions taken on protocol records;</li>
        <li>To improve the Service&rsquo;s reliability and content quality.</li>
      </ul>

      <h2>3. Third-Party Processors</h2>
      <p>We rely on the following subprocessors to operate the Service:</p>
      <ul>
        <li><strong>Anthropic</strong> &mdash; processes drug and study-parameter information you submit in order to generate protocol section drafts.</li>
        <li><strong>Clerk</strong> &mdash; handles authentication (sign-up, sign-in, session management).</li>
        <li><strong>Railway</strong> &mdash; hosts our application backend and PostgreSQL database.</li>
        <li><strong>Vercel</strong> &mdash; hosts our web application frontend.</li>
        <li><strong>NCBI PubMed / DailyMed (NIH)</strong> &mdash; public literature and drug-label sources queried to ground generated content; no account data is sent to these sources.</li>
      </ul>
      <p>
        We do not sell your personal information, and we do not use your protocol content to train models on
        behalf of other customers.
      </p>

      <h2>4. Data Isolation</h2>
      <p>
        Protocol and organization data is scoped server-side to your organization for every read and write
        operation. Members of one organization cannot access another organization&rsquo;s protocols, drafts, or
        team roster.
      </p>

      <h2>5. Data Retention</h2>
      <p>
        Account and protocol data is retained for as long as your organization&rsquo;s license is active, plus a
        reasonable period afterward to support account recovery and audit requirements. Sales inquiry records
        submitted via the &ldquo;Contact sales&rdquo; form are retained until the inquiry is resolved or you
        request deletion.
      </p>

      <h2>6. Your Choices</h2>
      <p>
        You may request access to, correction of, or deletion of your personal information by contacting us
        through the &ldquo;Contact sales&rdquo; form on our <a href="/#pricing">pricing page</a>. Organization
        owners can also manage team membership directly within the Service.
      </p>

      <h2>7. Security</h2>
      <p>
        We use role-based access control, verified session-based authentication, and server-side organization
        isolation to protect your data. No method of transmission or storage is completely secure, and we cannot
        guarantee absolute security.
      </p>

      <h2>8. Children&rsquo;s Privacy</h2>
      <p>The Service is intended for business use by adults working in regulatory and clinical research roles and is not directed to children.</p>

      <h2>9. Changes to This Policy</h2>
      <p>
        We may update this Privacy Policy from time to time. Material changes will be reflected by updating the
        &ldquo;Last updated&rdquo; date above.
      </p>

      <h2>10. Contact</h2>
      <p>
        Questions about this Privacy Policy can be sent through the &ldquo;Contact sales&rdquo; form on our
        <a href="/#pricing"> pricing page</a>.
      </p>
    </LegalLayout>
  )
}
