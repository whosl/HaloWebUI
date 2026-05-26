<script lang="ts">
	import { getContext } from 'svelte';
	import { ChevronDown, ChevronRight, Loader2, MessageCircle } from 'lucide-svelte';

	const i18n = getContext('i18n');

	export let discussion: any = null;

	let expanded = false;

	const toArray = (value: any) => (Array.isArray(value) ? value : []);

	$: participants = toArray(discussion?.participants);
	$: rounds = toArray(discussion?.rounds);
	$: status = discussion?.status ?? 'running';
	$: allTurns = rounds.flatMap((round) =>
		toArray(round?.turns).map((turn) => ({
			...turn,
			roundIndex: round?.index ?? turn?.round
		}))
	);
	$: latestTurns = allTurns
		.filter((turn) => turn?.content || turn?.error || turn?.status === 'running')
		.slice(-2);
	$: running = status === 'running' || status === 'summarizing' || allTurns.some((turn) => turn?.status === 'running');

	const statusLabel = (value: string) => {
		switch (value) {
			case 'completed':
				return $i18n.t('Completed');
			case 'stopped':
				return $i18n.t('Stopped');
			case 'error':
				return $i18n.t('Error');
			case 'summarizing':
				return $i18n.t('Summarizing');
			default:
				return $i18n.t('Discussing');
		}
	};

	const turnStatusLabel = (turn: any) => {
		if (turn?.status === 'error') {
			return $i18n.t('Failed');
		}
		if (turn?.status === 'running') {
			return $i18n.t('Thinking');
		}
		return $i18n.t('Done');
	};

	const turnText = (turn: any) => {
		const error = `${turn?.error ?? ''}`.trim();
		if (error) {
			return error;
		}
		return `${turn?.content ?? ''}`.trim();
	};

	const numberValue = (value: any): number | null => {
		if (typeof value === 'number' && Number.isFinite(value)) {
			return value;
		}
		const parsed = Number(value);
		return Number.isFinite(parsed) ? parsed : null;
	};

	const formatNumber = (value: number, digits = 0) =>
		value.toLocaleString(undefined, {
			maximumFractionDigits: digits,
			minimumFractionDigits: digits
		});

	const turnUsageStats = (turn: any) => {
		const usage = turn?.usage && typeof turn.usage === 'object' ? turn.usage : null;
		if (!usage) {
			return [];
		}

		const input = numberValue(usage.prompt_tokens ?? usage.input_tokens ?? usage.prompt_eval_count);
		const output = numberValue(usage.completion_tokens ?? usage.output_tokens ?? usage.eval_count);
		const total = numberValue(usage.total_tokens) ?? ((input ?? 0) + (output ?? 0) || null);
		const totalDuration = numberValue(usage.total_duration);
		const durationSeconds =
			numberValue(usage.duration_seconds) ??
			(totalDuration && totalDuration > 0 ? totalDuration / 1_000_000_000 : null);
		const speed =
			numberValue(usage.tokens_per_second ?? usage['response_token/s']) ??
			(output && durationSeconds && durationSeconds > 0 ? output / durationSeconds : null);

		const stats: string[] = [];
		if (total && total > 0) {
			stats.push(`${$i18n.t('Tokens')}: ${formatNumber(total)}`);
		}
		if (durationSeconds && durationSeconds > 0) {
			stats.push(`${$i18n.t('Elapsed')}: ${$i18n.t('{{seconds}} sec', { seconds: formatNumber(durationSeconds, durationSeconds < 10 ? 2 : 1) })}`);
		}
		if (speed && speed > 0) {
			stats.push(`${$i18n.t('Speed')}: ${formatNumber(speed, 1)} ${$i18n.t('tokens/s')}`);
		}

		return stats;
	};

	const statusClass = (value: string) => {
		switch (value) {
			case 'completed':
				return 'border-green-200 bg-green-50 text-green-700 dark:border-green-800/60 dark:bg-green-900/20 dark:text-green-300';
			case 'stopped':
				return 'border-gray-200 bg-gray-50 text-gray-600 dark:border-gray-700 dark:bg-gray-900/50 dark:text-gray-300';
			case 'error':
				return 'border-red-200 bg-red-50 text-red-700 dark:border-red-800/60 dark:bg-red-900/20 dark:text-red-300';
			default:
				return 'border-primary-200 bg-primary-50 text-primary-700 dark:border-primary-800/60 dark:bg-primary-900/20 dark:text-primary-200';
		}
	};
</script>

{#if discussion?.enabled}
	<div class="my-2 rounded-2xl border border-gray-200/80 bg-white/70 p-3 text-sm shadow-xs dark:border-gray-700/60 dark:bg-gray-900/45">
		<div class="flex items-start justify-between gap-3">
			<div class="min-w-0 flex-1">
				<div class="flex flex-wrap items-center gap-2">
					<div class="flex items-center gap-1.5 font-medium text-gray-800 dark:text-gray-100">
						<MessageCircle class="size-4 text-primary-600 dark:text-primary-300" />
						<span>{$i18n.t('Multi-model discussion')}</span>
					</div>

					<span class="inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium {statusClass(status)}">
						{#if running}
							<Loader2 class="size-3 animate-spin" />
						{/if}
						{statusLabel(status)}
					</span>
				</div>

				{#if participants.length > 0}
					<div class="mt-2 flex flex-wrap gap-1.5">
						{#each participants as participant}
							<span class="max-w-[180px] truncate rounded-full bg-gray-100 px-2 py-0.5 text-xs text-gray-600 dark:bg-gray-800 dark:text-gray-300">
								{participant?.name ?? participant?.id}
							</span>
						{/each}
					</div>
				{/if}
			</div>

			<button
				type="button"
				class="inline-flex shrink-0 items-center gap-1 rounded-full px-2 py-1 text-xs text-gray-500 hover:bg-gray-100 hover:text-gray-700 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-gray-200"
				on:click={() => {
					expanded = !expanded;
				}}
			>
				{#if expanded}
					<ChevronDown class="size-3.5" />
					{$i18n.t('Collapse')}
				{:else}
					<ChevronRight class="size-3.5" />
					{$i18n.t('Details')}
				{/if}
			</button>
		</div>

		<div class="mt-3 space-y-2" aria-live="polite">
			{#if latestTurns.length > 0}
				{#each latestTurns as turn}
					{@const usageStats = turnUsageStats(turn)}
					<div class="rounded-xl bg-gray-50/80 px-3 py-2 dark:bg-gray-800/55">
						<div class="flex flex-wrap items-center justify-between gap-2 text-xs">
							<div class="min-w-0 truncate font-medium text-gray-700 dark:text-gray-200">
								{$i18n.t('Round {{round}}', { round: turn?.roundIndex ?? turn?.round ?? '-' })} · {turn?.modelName ?? turn?.model ?? $i18n.t('Model')}
							</div>
							<div class="flex shrink-0 flex-wrap items-center justify-end gap-1.5 text-[11px] text-gray-400 dark:text-gray-500">
								{#each usageStats as stat}
									<span class="rounded-full bg-white/70 px-1.5 py-0.5 dark:bg-gray-900/55">{stat}</span>
								{/each}
								<span>{turnStatusLabel(turn)}</span>
							</div>
						</div>
						{#if turnText(turn)}
							<div class="mt-1 line-clamp-2 whitespace-pre-wrap text-xs leading-5 text-gray-600 dark:text-gray-300">
								{turnText(turn)}
							</div>
						{:else if turn?.status === 'running'}
							<div class="mt-1 flex items-center gap-1.5 text-xs text-gray-500 dark:text-gray-400">
								<Loader2 class="size-3 animate-spin" />
								{$i18n.t('This model is preparing its view...')}
							</div>
						{/if}
					</div>
				{/each}
			{:else}
				<div class="flex items-center gap-2 rounded-xl bg-gray-50/80 px-3 py-2 text-xs text-gray-500 dark:bg-gray-800/55 dark:text-gray-400">
					<Loader2 class="size-3 animate-spin" />
					{$i18n.t('Waiting for the first discussion turn...')}
				</div>
			{/if}
		</div>

		{#if expanded}
			<div class="mt-3 border-t border-gray-200/70 pt-3 dark:border-gray-700/60">
				{#if rounds.length > 0}
					<div class="space-y-3">
						{#each rounds as round}
							<div>
								<div class="mb-1 text-xs font-semibold text-gray-500 dark:text-gray-400">
									{$i18n.t('Round {{round}}', { round: round?.index ?? '-' })}
								</div>
								<div class="space-y-2">
									{#each toArray(round?.turns) as turn}
										{@const usageStats = turnUsageStats(turn)}
										<div class="rounded-xl border border-gray-200/70 px-3 py-2 dark:border-gray-700/60">
											<div class="flex flex-wrap items-center justify-between gap-2 text-xs">
												<span class="font-medium text-gray-700 dark:text-gray-200">{turn?.modelName ?? turn?.model ?? $i18n.t('Model')}</span>
												<div class="flex flex-wrap items-center justify-end gap-1.5 text-[11px] text-gray-400 dark:text-gray-500">
													{#each usageStats as stat}
														<span class="rounded-full bg-gray-50 px-1.5 py-0.5 dark:bg-gray-800/70">{stat}</span>
													{/each}
													<span>{turnStatusLabel(turn)}</span>
												</div>
											</div>
											<div class="mt-1 whitespace-pre-wrap text-xs leading-5 text-gray-600 dark:text-gray-300">
												{turnText(turn) || $i18n.t('No content yet.')}
											</div>
										</div>
									{/each}
								</div>
							</div>
						{/each}
					</div>
				{:else}
					<div class="text-xs text-gray-500 dark:text-gray-400">{$i18n.t('No discussion turns yet.')}</div>
				{/if}
			</div>
		{/if}
	</div>
{/if}
