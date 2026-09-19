import Markdown, { type Components } from 'react-markdown'
import remarkBreaks from 'remark-breaks'
import remarkGfm from 'remark-gfm'

function safeUrl(value: string, key: string) {
  if (key !== 'href') return undefined
  try {
    const url = new URL(value)
    return ['http:', 'https:', 'mailto:'].includes(url.protocol) ? url.href : undefined
  } catch { return undefined }
}

const components: Components = {
  a: ({ href, title, children }) => href ? <a href={href} title={title} target="_blank" rel="noopener noreferrer">{children}</a> : <span>{children}</span>,
  img: ({ alt }) => <span className="markdown-image-caption">{alt || 'Изображение'}</span>,
  input: ({ checked }) => <input type="checkbox" checked={Boolean(checked)} disabled readOnly aria-label={checked ? 'Выполнено' : 'Не выполнено'} />,
  pre: ({ children }) => <pre tabIndex={0} aria-label="Блок кода">{children}</pre>,
  table: ({ children }) => <div className="markdown-table" tabIndex={0} role="region" aria-label="Таблица"><table>{children}</table></div>,
}

export function MessageContent({ content }: { content: string }) {
  return <div className="markdown-message"><Markdown remarkPlugins={[remarkGfm, remarkBreaks]} components={components} urlTransform={safeUrl}>{content}</Markdown></div>
}
