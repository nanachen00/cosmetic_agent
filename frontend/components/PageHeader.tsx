export function PageHeader({
  eyebrow,
  title,
  description
}: {
  eyebrow?: string;
  title: string;
  description?: string;
}) {
  return (
    <header className="page-header">
      {eyebrow ? <span className="eyebrow-badge">✦ {eyebrow} ✦</span> : null}
      <h1>{title}</h1>
      {description ? <p>{description}</p> : null}
      <div className="lace-divider" aria-hidden="true"><span>♡</span></div>
    </header>
  );
}
