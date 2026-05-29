import { resolveSafeMarkdownUrl } from './html-safety';

type CssValue = string | number | null | undefined;
type ParsedBlock =
	| { type: 'heading'; level: number; text: string }
	| { type: 'paragraph'; text: string }
	| { type: 'list'; ordered: boolean; items: string[] }
	| { type: 'quote'; text: string }
	| { type: 'code'; lang: string; text: string }
	| { type: 'table'; headers: string[]; rows: string[][] };

const THEME = {
	bg: '#f8fafc',
	surface: '#ffffff',
	panel: '#f1f5f9',
	text: '#0f172a',
	muted: '#64748b',
	primary: '#2563eb',
	primarySoft: '#eff6ff',
	accent: '#f59e0b',
	accentSoft: '#fffbeb',
	border: '#cbd5e1',
	borderSubtle: '#e2e8f0',
	codeBg: '#0f172a',
	codeText: '#e2e8f0',
	shadow: '0 4px 6px -1px rgba(15, 23, 42, 0.06), 0 2px 4px -2px rgba(15, 23, 42, 0.06)'
};

const normalizeText = (value: unknown) =>
	String(value ?? '')
		.replace(/\r\n?|\u2028|\u2029/g, '\n')
		.trim();

const escapeHtml = (value: unknown) =>
	String(value ?? '')
		.replaceAll('&', '&amp;')
		.replaceAll('<', '&lt;')
		.replaceAll('>', '&gt;')
		.replaceAll('"', '&quot;')
		.replaceAll("'", '&#039;');

const escapeAttribute = (value: unknown) => escapeHtml(value).replaceAll('`', '&#096;');

const toStyle = (rules: Record<string, CssValue>) =>
	Object.entries(rules)
		.filter(([, value]) => value !== null && value !== undefined && value !== '')
		.map(([key, value]) => `${key}: ${value}`)
		.join('; ');

const compactHtml = (html: string) => html.split('\n').map((line) => line.trim()).join('');

const isFence = (line: string) => /^\s*```/.test(line);
const isHeading = (line: string) => /^#{1,4}\s+\S/.test(line);
const isQuote = (line: string) => /^\s*>\s?/.test(line);
const isUnorderedList = (line: string) => /^\s*[-*+]\s+\S/.test(line);
const isOrderedList = (line: string) => /^\s*\d+[.)]\s+\S/.test(line);
const isList = (line: string) => isUnorderedList(line) || isOrderedList(line);
const isTableSeparator = (line: string) =>
	/^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(line);
const looksLikeTableRow = (line: string) => line.includes('|') && !isFence(line);

const stripTableEdges = (line: string) => line.trim().replace(/^\|/, '').replace(/\|$/, '');
const parseTableRow = (line: string) => stripTableEdges(line).split('|').map((cell) => cell.trim());

const renderInlineWithoutLinks = (value: string): string => {
	const input = normalizeText(value);
	let html = '';
	let cursor = 0;
	const codePattern = /`([^`\n]+)`/g;
	let match: RegExpExecArray | null;

	const renderEmphasis = (text: string) =>
		escapeHtml(text)
			.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
			.replace(/__([^_]+)__/g, '<strong>$1</strong>')
			.replace(/\n+/g, '<br>');

	while ((match = codePattern.exec(input))) {
		html += renderEmphasis(input.slice(cursor, match.index));
		html += `<code style="${escapeAttribute(
			toStyle({
				padding: '1px 5px',
				'border-radius': '6px',
				background: THEME.panel,
				color: THEME.text,
				'font-size': '0.92em'
			})
		)}">${escapeHtml(match[1])}</code>`;
		cursor = match.index + match[0].length;
	}

	html += renderEmphasis(input.slice(cursor));
	return html;
};

const renderInline = (value: string): string => {
	const input = normalizeText(value);
	let html = '';
	let cursor = 0;
	const linkPattern = /\[([^\]\n]{1,200})\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g;
	let match: RegExpExecArray | null;

	while ((match = linkPattern.exec(input))) {
		html += renderInlineWithoutLinks(input.slice(cursor, match.index));
		const label = match[1];
		const href = resolveSafeMarkdownUrl(match[2], { allowHash: true, allowRelative: false });

		if (href) {
			html += `<a href="${escapeAttribute(href)}" target="_blank" rel="noopener noreferrer nofollow" style="${escapeAttribute(
				toStyle({ color: THEME.primary, 'text-decoration': 'none', 'font-weight': 600 })
			)}">${renderInlineWithoutLinks(label)}</a>`;
		} else {
			html += renderInlineWithoutLinks(match[0]);
		}

		cursor = match.index + match[0].length;
	}

	html += renderInlineWithoutLinks(input.slice(cursor));
	return html;
};

const parseBlocks = (content: string): ParsedBlock[] => {
	const lines = normalizeText(content).split('\n');
	const blocks: ParsedBlock[] = [];
	let i = 0;

	const readParagraph = () => {
		const paragraph: string[] = [];
		while (i < lines.length) {
			const line = lines[i];
			if (
				!line.trim() ||
				isFence(line) ||
				isHeading(line) ||
				isQuote(line) ||
				isList(line) ||
				(looksLikeTableRow(line) && i + 1 < lines.length && isTableSeparator(lines[i + 1]))
			) {
				break;
			}

			paragraph.push(line.trim());
			i += 1;
		}

		if (paragraph.length > 0) {
			blocks.push({ type: 'paragraph', text: paragraph.join('\n') });
		}
	};

	while (i < lines.length) {
		const line = lines[i];
		if (!line.trim()) {
			i += 1;
			continue;
		}

		if (isFence(line)) {
			const lang = line.replace(/^\s*```/, '').trim();
			const code: string[] = [];
			i += 1;
			while (i < lines.length && !isFence(lines[i])) {
				code.push(lines[i]);
				i += 1;
			}
			if (i < lines.length) i += 1;
			blocks.push({ type: 'code', lang, text: code.join('\n') });
			continue;
		}

		if (looksLikeTableRow(line) && i + 1 < lines.length && isTableSeparator(lines[i + 1])) {
			const headers = parseTableRow(line);
			const rows: string[][] = [];
			i += 2;
			while (i < lines.length && looksLikeTableRow(lines[i]) && lines[i].trim()) {
				rows.push(parseTableRow(lines[i]));
				i += 1;
			}
			blocks.push({ type: 'table', headers, rows });
			continue;
		}

		if (isHeading(line)) {
			const [, marker = '#', text = ''] = /^(#{1,4})\s+(.*)$/.exec(line.trim()) ?? [];
			blocks.push({ type: 'heading', level: marker.length, text });
			i += 1;
			continue;
		}

		if (isQuote(line)) {
			const quote: string[] = [];
			while (i < lines.length && isQuote(lines[i])) {
				quote.push(lines[i].replace(/^\s*>\s?/, '').trim());
				i += 1;
			}
			blocks.push({ type: 'quote', text: quote.join('\n') });
			continue;
		}

		if (isList(line)) {
			const ordered = isOrderedList(line);
			const items: string[] = [];
			while (i < lines.length && (ordered ? isOrderedList(lines[i]) : isUnorderedList(lines[i]))) {
				items.push(lines[i].replace(/^\s*(?:[-*+]|\d+[.)])\s+/, '').trim());
				i += 1;
			}
			blocks.push({ type: 'list', ordered, items });
			continue;
		}

		readParagraph();
	}

	return blocks;
};

const getBlockPlainText = (block: ParsedBlock): string => {
	switch (block.type) {
		case 'heading':
		case 'paragraph':
		case 'quote':
		case 'code':
			return block.text;
		case 'list':
			return block.items.join(' ');
		case 'table':
			return [...block.headers, ...block.rows.flat()].join(' ');
	}
};

const truncateText = (value: string, length = 180) => {
	const text = normalizeText(value).replace(/\s+/g, ' ');
	return text.length > length ? `${text.slice(0, length - 1)}...` : text;
};

const renderCard = (inner: string, extraStyle: Record<string, CssValue> = {}) =>
	`<section style="${escapeAttribute(
		toStyle({
			background: THEME.surface,
			border: `1px solid ${THEME.borderSubtle}`,
			'border-radius': '14px',
			padding: '16px',
			'box-shadow': '0 1px 2px rgba(15, 23, 42, 0.04)',
			...extraStyle
		})
	)}">${inner}</section>`;

const renderBlock = (block: ParsedBlock) => {
	switch (block.type) {
		case 'heading':
			return renderCard(
				`<h3 style="${escapeAttribute(
					toStyle({ margin: 0, color: THEME.text, 'font-size': '18px', 'font-weight': 750 })
				)}">${renderInline(block.text)}</h3>`,
				{ 'border-left': `4px solid ${THEME.primary}` }
			);

		case 'paragraph':
			return renderCard(
				`<p style="${escapeAttribute(
					toStyle({ margin: 0, color: THEME.text, 'font-size': '14px', 'line-height': 1.75 })
				)}">${renderInline(block.text)}</p>`
			);

		case 'quote':
			return renderCard(
				`<blockquote style="${escapeAttribute(
					toStyle({ margin: 0, color: THEME.text, 'font-size': '14px', 'line-height': 1.75 })
				)}">${renderInline(block.text)}</blockquote>`,
				{ background: THEME.accentSoft, border: `1px solid ${THEME.accent}` }
			);

		case 'list': {
			const tag = block.ordered ? 'ol' : 'ul';
			const items = block.items
				.map(
					(item) =>
						`<li style="${escapeAttribute(toStyle({ margin: '6px 0', padding: 0 }))}">${renderInline(item)}</li>`
				)
				.join('');

			return renderCard(
				`<${tag} style="${escapeAttribute(
					toStyle({ margin: 0, padding: block.ordered ? '0 0 0 22px' : '0 0 0 20px', color: THEME.text, 'font-size': '14px', 'line-height': 1.7 })
				)}">${items}</${tag}>`
			);
		}

		case 'code':
			return renderCard(
				`${
					block.lang
						? `<div style="${escapeAttribute(toStyle({ color: THEME.muted, 'font-size': '12px', 'margin-bottom': '8px' }))}">${escapeHtml(block.lang)}</div>`
						: ''
				}<pre style="${escapeAttribute(
					toStyle({ margin: 0, padding: '14px', overflow: 'auto', background: THEME.codeBg, color: THEME.codeText, 'border-radius': '12px', 'font-size': '13px', 'line-height': 1.6 })
				)}"><code>${escapeHtml(block.text)}</code></pre>`,
				{ padding: '12px' }
			);

		case 'table': {
			const headers = block.headers
				.map(
					(header) =>
						`<th style="${escapeAttribute(toStyle({ padding: '8px 10px', border: `1px solid ${THEME.borderSubtle}`, background: THEME.primarySoft, color: THEME.text, 'font-size': '12px', 'text-align': 'left' }))}">${renderInline(header)}</th>`
				)
				.join('');
			const rows = block.rows
				.map(
					(row) =>
						`<tr>${row
							.map(
								(cell) =>
									`<td style="${escapeAttribute(toStyle({ padding: '8px 10px', border: `1px solid ${THEME.borderSubtle}`, color: THEME.text, 'font-size': '13px', 'vertical-align': 'top' }))}">${renderInline(cell)}</td>`
							)
							.join('')}</tr>`
				)
				.join('');

			return renderCard(
				`<div style="${escapeAttribute(toStyle({ overflow: 'auto', 'max-width': '100%' }))}"><table style="${escapeAttribute(toStyle({ width: '100%', 'border-collapse': 'collapse' }))}"><thead><tr>${headers}</tr></thead><tbody>${rows}</tbody></table></div>`,
				{ padding: '12px' }
			);
		}
	}
};

export const isKnownInlineHtmlFormatFragment = (content: unknown): content is string =>
	typeof content === 'string' &&
	/(data-html-render-mcp\s*=\s*["']inline["']|data-halo-response-html-format\s*=\s*["']inline["'])/i.test(
		content
	);

export const renderResponseHtmlFormat = (content: string): string => {
	const normalized = normalizeText(content);
	if (!normalized) {
		return '';
	}

	if (isKnownInlineHtmlFormatFragment(normalized)) {
		return normalized;
	}

	const blocks = parseBlocks(normalized);
	if (blocks.length === 0) {
		return '';
	}

	const titleBlock = blocks.find((block) => block.type === 'heading') ?? blocks[0];
	const title = titleBlock.type === 'heading' ? titleBlock.text : truncateText(getBlockPlainText(titleBlock), 80);
	const subtitleBlock = blocks.find((block) => block.type === 'paragraph');
	const subtitle = subtitleBlock ? truncateText(subtitleBlock.text) : '';
	const bodyBlocks = blocks.filter((block, index) => !(index === 0 && block === titleBlock));

	const hero = `<div style="${escapeAttribute(
		toStyle({
			padding: '20px',
			background: `linear-gradient(135deg, ${THEME.primarySoft}, ${THEME.surface})`,
			border: `1px solid ${THEME.border}`,
			'border-radius': '16px',
			'box-shadow': THEME.shadow
		})
	)}"><div style="${escapeAttribute(
		toStyle({ color: THEME.primary, 'font-size': '12px', 'font-weight': 700, 'letter-spacing': '0.08em', 'text-transform': 'uppercase', 'margin-bottom': '8px' })
	)}">HTML FORMAT</div><h2 style="${escapeAttribute(
		toStyle({ margin: 0, color: THEME.text, 'font-size': '22px', 'line-height': 1.35, 'font-weight': 800 })
	)}">${renderInline(title)}</h2>${
		subtitle
			? `<p style="${escapeAttribute(toStyle({ margin: '10px 0 0', color: THEME.muted, 'font-size': '14px', 'line-height': 1.7 }))}">${renderInline(subtitle)}</p>`
			: ''
	}</div>`;

	const body = bodyBlocks.map(renderBlock).join('');
	const root = `<div data-halo-response-html-format="inline" style="${escapeAttribute(
		toStyle({
			margin: '8px 0',
			padding: '14px',
			background: THEME.bg,
			color: THEME.text,
			border: `1px solid ${THEME.border}`,
			'border-radius': '18px',
			'font-family': "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif",
			'line-height': 1.6,
			'max-width': '100%',
			overflow: 'hidden'
		})
	)}">${hero}<div style="${escapeAttribute(toStyle({ display: 'grid', gap: '10px', 'margin-top': '12px' }))}">${body}</div></div>`;

	return compactHtml(root);
};
