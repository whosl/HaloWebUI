import { describe, expect, it } from 'vitest';

import {
	buildWebSearchModeOptions,
	resolveConfiguredDefaultWebSearchMode
} from './native-web-search';

const t = (key: string) => key;

describe('native web search mode options', () => {
	it('offers smart web search when only HaloWebUI search is enabled', () => {
		const options = buildWebSearchModeOptions(
			t,
			{
				features: {
					enable_halo_web_search: true,
					enable_native_web_search: false
				}
			},
			[{ id: 'local-model', owned_by: 'anthropic' }]
		);

		expect(options.map((option) => option.value)).toContain('auto');
		expect(options.find((option) => option.value === 'auto')?.disabled).toBeFalsy();
		expect(options.find((option) => option.value === 'native')).toBeUndefined();
	});

	it('keeps smart web search disabled when no route can search', () => {
		const options = buildWebSearchModeOptions(
			t,
			{
				features: {
					enable_halo_web_search: false,
					enable_native_web_search: true
				}
			},
			[{ id: 'local-model', owned_by: 'anthropic' }]
		);

		expect(options.find((option) => option.value === 'auto')?.disabled).toBe(true);
	});

	it('keeps new chats off when the admin default is missing or off', () => {
		expect(
			resolveConfiguredDefaultWebSearchMode(
				t,
				{
					features: {
						enable_halo_web_search: true,
						enable_native_web_search: true
					}
				},
				[{ id: 'gpt-5', owned_by: 'openai' }],
				true
			)
		).toBe('off');

		expect(
			resolveConfiguredDefaultWebSearchMode(
				t,
				{
					features: {
						enable_halo_web_search: true,
						enable_native_web_search: true,
						default_web_search_mode: 'off'
					}
				},
				[{ id: 'gpt-5', owned_by: 'openai' }],
				true
			)
		).toBe('off');
	});

	it('uses the configured smart default only when a search route is available', () => {
		expect(
			resolveConfiguredDefaultWebSearchMode(
				t,
				{
					features: {
						enable_halo_web_search: true,
						enable_native_web_search: false,
						default_web_search_mode: 'auto'
					}
				},
				[{ id: 'local-model', owned_by: 'anthropic' }],
				true
			)
		).toBe('auto');

		expect(
			resolveConfiguredDefaultWebSearchMode(
				t,
				{
					features: {
						enable_halo_web_search: false,
						enable_native_web_search: false,
						default_web_search_mode: 'auto'
					}
				},
				[{ id: 'local-model', owned_by: 'anthropic' }],
				true
			)
		).toBe('off');
	});

	it('falls back to off when the configured native default cannot be used', () => {
		expect(
			resolveConfiguredDefaultWebSearchMode(
				t,
				{
					features: {
						enable_halo_web_search: true,
						enable_native_web_search: false,
						default_web_search_mode: 'native'
					}
				},
				[{ id: 'gpt-5', owned_by: 'openai' }],
				true
			)
		).toBe('off');

		expect(
			resolveConfiguredDefaultWebSearchMode(
				t,
				{
					features: {
						enable_halo_web_search: true,
						enable_native_web_search: true,
						default_web_search_mode: 'native'
					}
				},
				[{ id: 'local-model', owned_by: 'anthropic' }],
				true
			)
		).toBe('off');
	});

	it('keeps new chats off when the user cannot use web search', () => {
		expect(
			resolveConfiguredDefaultWebSearchMode(
				t,
				{
					features: {
						enable_halo_web_search: true,
						enable_native_web_search: true,
						default_web_search_mode: 'auto'
					}
				},
				[{ id: 'gpt-5', owned_by: 'openai' }],
				false
			)
		).toBe('off');
	});
});
