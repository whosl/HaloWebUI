import { describe, expect, it } from 'vitest';

import { resolveAvailableChatModelSelectionValues } from './chat-model-recovery';

describe('resolveAvailableChatModelSelectionValues', () => {
	it('drops stale persisted model ids instead of preserving unavailable selections', () => {
		const result = resolveAvailableChatModelSelectionValues(
			[
				{
					id: 'pipe_a.model_id_1',
					name: 'model_1',
					selection_id: 'modelref::pipe::function::id:pipe_a::model_id_1',
					model_id: 'model_id_1',
					model_ref: {
						provider: 'pipe',
						source: 'function',
						connection_id: 'pipe_a'
					}
				}
			],
			['modelref::pipe::function::id:old_pipe::model_id_1', 'pipe_a.model_id_1']
		);

		expect(result.values).toEqual(['modelref::pipe::function::id:pipe_a::model_id_1']);
		expect(result.droppedUnavailable).toBe(true);
		expect(result.resolutions.map((resolution) => resolution.status)).toEqual([
			'stale',
			'resolved'
		]);
	});
});
