import { describe, expect, it } from 'vitest';

import {
	isKnownInlineHtmlFormatFragment,
	renderResponseHtmlFormat
} from './response-html-format';

describe('response-html-format', () => {
	it('renders markdown-like content as a Halo inline HTML fragment', () => {
		const html = renderResponseHtmlFormat('# Report\n\nSummary text\n\n- one\n- two');

		expect(html).toContain('data-halo-response-html-format="inline"');
		expect(html).toContain('Report');
		expect(html).toContain('Summary text');
		expect(html).toContain('<li');
	});

	it('escapes raw html from model output', () => {
		const html = renderResponseHtmlFormat('Hello <script>alert(1)</script>');

		expect(html).toContain('&lt;script&gt;alert(1)&lt;/script&gt;');
		expect(html).not.toContain('<script>');
	});

	it('does not emit unsafe markdown link hrefs', () => {
		const html = renderResponseHtmlFormat(
			'[bad](javascript:alert(1)) [ok](https://example.com)'
		);

		expect(html).not.toContain('href="javascript:');
		expect(html).toContain('href="https://example.com"');
	});

	it('passes through known inline html fragments for downstream sanitization', () => {
		const fragment = '<div data-html-render-mcp="inline"><b>Ready</b></div>';

		expect(isKnownInlineHtmlFormatFragment(fragment)).toBe(true);
		expect(renderResponseHtmlFormat(fragment)).toBe(fragment);
	});
});
