<script lang="ts">
	import { getContext, onMount, tick } from 'svelte';
	import { toast } from 'svelte-sonner';

	import {
		getImageGenerationModels,
		getImageUsageConfig,
		updateImageGenerationConfig,
		imageGenerations
	} from '$lib/apis/images';
	import type {
		ImageGenerationConfig,
		ImageGenerationModel,
		ImageUsageConfig
	} from '$lib/apis/images';
	import HaloSelect from '$lib/components/common/HaloSelect.svelte';
	import ImagePreview from '$lib/components/common/ImagePreview.svelte';
	import ArrowDownTray from '$lib/components/icons/ArrowDownTray.svelte';
	import ArrowsPointingOut from '$lib/components/icons/ArrowsPointingOut.svelte';
	import Clipboard from '$lib/components/icons/Clipboard.svelte';
	import PhotoSolid from '$lib/components/icons/PhotoSolid.svelte';
	import Sparkles from '$lib/components/icons/Sparkles.svelte';
	import { WEBUI_NAME, user } from '$lib/stores';
	import { copyToClipboard } from '$lib/utils';
	import { localizeCommonError } from '$lib/utils/common-errors';

	type GeneratedImage = {
		url: string;
	};

	type ViewState = 'loading' | 'ready' | 'disabled' | 'denied' | 'error';

	type SizeOption = {
		value: string;
		ratio: string;
		label: string;
	};

	const i18n = getContext('i18n');

	const promptIdeas = [
		'Cinematic portrait',
		'Clean product shot',
		'Editorial poster',
		'Cozy illustration',
		'Neon cityscape',
		'Minimal interior'
	];

	const curatedSizeOptions: SizeOption[] = [
		{ value: '1024x1024', ratio: '1:1', label: '1024x1024' },
		{ value: '1024x1536', ratio: '2:3', label: '1024x1536' },
		{ value: '1536x1024', ratio: '3:2', label: '1536x1024' }
	];

	let loaded = false;
	let loading = false;
	let savingDefaults = false;
	let viewState: ViewState = 'loading';
	let loadError: string | null = null;
	let usageConfig: ImageUsageConfig | null = null;
	let imageModels: ImageGenerationModel[] = [];
	let prompt = '';
	let selectedModel = '';
	let selectedSize = '512x512';
	let steps = 0;
	let canSubmit = false;
	let blockedReason: string | null = null;
	let workspaceNoModels = false;

	let generatedImages: GeneratedImage[] = [];
	let lastPrompt = '';
	let resultsSectionElement: HTMLElement | null = null;

	let previewOpen = false;
	let previewSrc = '';
	let previewAlt = '';

	let savedDefaults: ImageGenerationConfig = {
		MODEL: '',
		IMAGE_SIZE: '512x512',
		IMAGE_STEPS: 0,
		IMAGE_MODEL_FILTER_REGEX: null
	};

	const isAdmin = () => $user?.role === 'admin';
	const formatError = (error: unknown) =>
		localizeCommonError(error, (key, options) => $i18n.t(key, options));

	$: modelOptions = imageModels.map((model) => ({
		value: model.id,
		label: model.name ?? model.id
	}));

	$: sizeOptions = curatedSizeOptions.some((option) => option.value === savedDefaults.IMAGE_SIZE)
		? curatedSizeOptions
		: [
				{
					value: savedDefaults.IMAGE_SIZE,
					ratio: $i18n.t('Default'),
					label: savedDefaults.IMAGE_SIZE
				},
				...curatedSizeOptions
			];

	$: selectedModelLabel =
		modelOptions.find((option) => option.value === selectedModel)?.label ?? selectedModel;
	$: selectedModelMeta = imageModels.find((model) => model.id === selectedModel) ?? null;
	$: selectedAspectRatio = (() => {
		const normalized = `${selectedSize ?? ''}`.trim().toLowerCase();
		const match = normalized.match(/^(\d+)x(\d+)$/);
		if (!match) return null;

		const width = Number(match[1]);
		const height = Number(match[2]);
		if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) {
			return null;
		}

		const gcd = (a: number, b: number): number => (b === 0 ? a : gcd(b, a % b));
		const divisor = gcd(width, height);
		return divisor > 0 ? `${width / divisor}:${height / divisor}` : null;
	})();

	$: workspaceDefaultsDirty =
		isAdmin() &&
		(selectedModel !== `${savedDefaults.MODEL ?? ''}`.trim() ||
			selectedSize !== `${savedDefaults.IMAGE_SIZE ?? ''}`.trim() ||
			steps !== Number(savedDefaults.IMAGE_STEPS ?? 0));

	$: canSubmit =
		!loading && viewState === 'ready' && imageModels.length > 0 && Boolean(prompt.trim());

	const applyPromptIdea = (idea: string) => {
		const translatedIdea = $i18n.t(idea);
		prompt = prompt.trim() ? `${prompt.trim()}, ${translatedIdea}` : translatedIdea;
	};

	const handleComposerKeydown = (event: KeyboardEvent) => {
		if ((event.metaKey || event.ctrlKey) && event.key === 'Enter' && !loading) {
			event.preventDefault();
			void submitHandler();
		}
	};

	const syncSelectedModelWithAvailableModels = (models: ImageGenerationModel[]) => {
		const availableIds = new Set(
			(models ?? []).map((model) => `${model?.id ?? ''}`.trim()).filter(Boolean)
		);

		if (selectedModel && availableIds.has(selectedModel)) {
			return;
		}

		const defaultModelId = `${savedDefaults.MODEL ?? ''}`.trim();
		selectedModel =
			[selectedModel, defaultModelId, models?.[0]?.id ?? ''].find(
				(candidate) => candidate && availableIds.has(candidate)
			) ?? '';
	};

	const applySharedDefaults = (defaults: ImageUsageConfig['defaults'] | null | undefined) => {
		const nextModel = `${defaults?.model ?? ''}`.trim();
		const nextSize = `${defaults?.size ?? '512x512'}`.trim() || '512x512';
		const nextSteps = Number(defaults?.steps ?? 0);

		savedDefaults = {
			MODEL: nextModel,
			IMAGE_SIZE: nextSize,
			IMAGE_STEPS: Number.isFinite(nextSteps) ? nextSteps : 0,
			IMAGE_MODEL_FILTER_REGEX: null
		};

		selectedModel = nextModel;
		selectedSize = nextSize;
		steps = Number.isFinite(nextSteps) ? nextSteps : 0;
	};

	const loadWorkspaceModels = async () => {
		const nextModels = await getImageGenerationModels(localStorage.token, {
			context: 'settings'
		}).catch((error) => {
			loadError = `${error ?? ''}`;
			return null;
		});

		imageModels = Array.isArray(nextModels) ? nextModels : [];
		syncSelectedModelWithAvailableModels(imageModels);
		workspaceNoModels = !loadError && imageModels.length === 0;
		if (workspaceNoModels) {
			blockedReason = $i18n.t('Image models are unavailable right now. Check your image settings.');
		}
	};

	const copyPromptHandler = async () => {
		const text = lastPrompt || prompt.trim();
		if (!text) return;

		const copied = await copyToClipboard(text);
		if (copied) {
			toast.success($i18n.t('Prompt copied'));
		}
	};

	const openPreview = (image: GeneratedImage, index: number) => {
		previewSrc = image.url;
		previewAlt = `${$i18n.t('Generated image')} ${index + 1}`;
		previewOpen = true;
	};

	const downloadImage = (url: string, index: number) => {
		const link = document.createElement('a');
		link.href = url;
		link.download = `generated-image-${index + 1}.png`;
		document.body.appendChild(link);
		link.click();
		document.body.removeChild(link);
	};

	const saveWorkspaceDefaults = async () => {
		if (!isAdmin() || !workspaceDefaultsDirty) return;

		savingDefaults = true;
		try {
			const updated = await updateImageGenerationConfig(localStorage.token, {
				MODEL: selectedModel || '',
				IMAGE_SIZE: selectedSize,
				IMAGE_STEPS: steps,
				IMAGE_MODEL_FILTER_REGEX: savedDefaults.IMAGE_MODEL_FILTER_REGEX ?? undefined
			});

			savedDefaults = {
				MODEL: `${updated?.MODEL ?? selectedModel ?? ''}`.trim(),
				IMAGE_SIZE: `${updated?.IMAGE_SIZE ?? selectedSize}`,
				IMAGE_STEPS: Number(updated?.IMAGE_STEPS ?? steps ?? 0),
				IMAGE_MODEL_FILTER_REGEX: updated?.IMAGE_MODEL_FILTER_REGEX ?? null
			};

			if (usageConfig) {
				usageConfig = {
					...usageConfig,
					defaults: {
						...usageConfig.defaults,
						model: savedDefaults.MODEL,
						size: savedDefaults.IMAGE_SIZE,
						steps: savedDefaults.IMAGE_STEPS
					}
				};
			}

			selectedModel = savedDefaults.MODEL;
			selectedSize = savedDefaults.IMAGE_SIZE;
			steps = savedDefaults.IMAGE_STEPS;
			toast.success($i18n.t('Settings saved successfully!'));
		} catch (error) {
			toast.error(formatError(error));
		} finally {
			savingDefaults = false;
		}
	};

	const submitHandler = async () => {
		const trimmedPrompt = prompt.trim();
		if (!trimmedPrompt) {
			toast.error($i18n.t('Please enter a prompt'));
			return;
		}

		if (!canSubmit) {
			if (blockedReason) {
				toast.error(blockedReason);
			}
			return;
		}

		loading = true;
		generatedImages = [];
		lastPrompt = trimmedPrompt;

		try {
			const response = await imageGenerations(localStorage.token, {
				prompt: trimmedPrompt,
				model: selectedModel || undefined,
				size: selectedSize || undefined,
				steps: steps > 0 ? steps : undefined,
				credential_source: 'shared'
			});

			if (response?.length) {
				generatedImages = response;
				await tick();
				resultsSectionElement?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
			} else {
				toast.error(
					$i18n.t('Model returned an empty response. Try resending or switching models.')
				);
			}
		} catch (error) {
			toast.error(formatError(error));
		} finally {
			loading = false;
		}
	};

	onMount(async () => {
		const allowed =
			$user?.role === 'admin' || Boolean($user?.permissions?.features?.image_generation);
		if (!allowed) {
			viewState = 'denied';
			loaded = true;
			return;
		}

		viewState = 'loading';
		loaded = true;

		const usageResult = await getImageUsageConfig(localStorage.token).catch((error) => error);

		if (
			usageResult instanceof Error ||
			!usageResult ||
			typeof usageResult !== 'object' ||
			!('enabled' in usageResult)
		) {
			loadError = `${usageResult ?? ''}`;
			blockedReason = loadError || $i18n.t('Failed to load image generation settings.');
			viewState = 'error';
			return;
		}

		usageConfig = usageResult as ImageUsageConfig;
		applySharedDefaults(usageConfig.defaults);

		if (!usageConfig.enabled) {
			blockedReason = $i18n.t('Image generation is disabled by the administrator.');
			viewState = 'disabled';
			return;
		}

		await loadWorkspaceModels();

		if (loadError) {
			blockedReason = loadError;
			viewState = 'error';
			return;
		}

		viewState = 'ready';
	});
</script>

<svelte:head>
	<title>{$i18n.t('Images')} | {$WEBUI_NAME}</title>
</svelte:head>

{#if loaded}
	{#if viewState !== 'ready' || workspaceNoModels}
		<div class="space-y-4">
			<section class="workspace-section space-y-4">
				<div class="flex flex-col gap-3 lg:flex-row lg:items-center">
					<div class="workspace-toolbar-summary">
						<div class="workspace-count-pill">
							<PhotoSolid className="size-3.5" />
							{$i18n.t('Image Studio')}
						</div>
						<div class="text-xs text-gray-500 dark:text-gray-400">
							{blockedReason ?? $i18n.t('Loading image generation settings...')}
						</div>
					</div>
				</div>
			</section>

			<section class="workspace-section">
				<div class="workspace-empty-state">
					<div class="flex size-14 mx-auto items-center justify-center rounded-2xl bg-gray-100 text-gray-400 dark:bg-gray-800 dark:text-gray-500">
						<PhotoSolid className="size-7" />
					</div>
					<h2 class="mt-4 text-base font-semibold text-gray-900 dark:text-white">
						{viewState === 'denied'
							? $i18n.t('Image generation access required')
							: viewState === 'disabled'
								? $i18n.t('Image generation is disabled')
								: viewState === 'error'
									? $i18n.t('Unable to load image generation')
								: workspaceNoModels
									? $i18n.t('No models available')
										: $i18n.t('Loading...')}
					</h2>
					<p class="mt-2 text-sm text-gray-500 dark:text-gray-400">
						{blockedReason ??
							(viewState === 'loading'
								? $i18n.t('Loading image generation settings...')
								: $i18n.t('Please try again later.'))}
					</p>
					<div class="mt-5 flex flex-wrap justify-center gap-2">
						{#if isAdmin()}
							<a class="workspace-secondary-button text-xs" href="/settings/images">
								{$i18n.t('Open image settings')}
							</a>
						{/if}
						<button type="button" class="workspace-secondary-button text-xs" on:click={() => location.reload()}>
							{$i18n.t('Refresh')}
						</button>
					</div>
				</div>
			</section>
		</div>
	{:else}
		<form class="space-y-4" on:submit|preventDefault={submitHandler}>
			<section class="workspace-section space-y-4">
				<div class="flex flex-col gap-3 lg:flex-row lg:items-center">
					<div class="workspace-toolbar-summary">
						<div class="workspace-count-pill">
							<PhotoSolid className="size-3.5" />
							{$i18n.t('Image Studio')}
						</div>
						<div class="text-xs text-gray-500 dark:text-gray-400">
							{$i18n.t('Create polished visuals from a single prompt.')}
							<span class="hidden sm:inline ml-1 opacity-70">
								{$i18n.t('Press Ctrl/Command + Enter to generate.')}
							</span>
						</div>
					</div>

					<div class="workspace-toolbar">
						{#if isAdmin()}
							<HaloSelect
								bind:value={selectedModel}
								options={modelOptions}
								placeholder={$i18n.t('Select a model')}
								className="w-full lg:w-48 text-xs"
							/>
						{:else}
							<div class="flex min-h-10 w-full items-center rounded-xl border border-gray-200/80 bg-white px-3 text-xs text-gray-600 dark:border-gray-700/50 dark:bg-gray-900/70 dark:text-gray-300 lg:w-48">
								{selectedModelLabel || $i18n.t('No models available')}
							</div>
						{/if}

						<div class="workspace-toolbar-actions">
							{#if isAdmin()}
								<button
									type="button"
									class="workspace-secondary-button"
									disabled={!workspaceDefaultsDirty || savingDefaults}
									on:click={saveWorkspaceDefaults}
								>
									<span>{savingDefaults ? $i18n.t('Saving...') : $i18n.t('Save')}</span>
								</button>
							{/if}

							<button
								type="submit"
								class="workspace-primary-button"
								disabled={!canSubmit}
								title={blockedReason ?? ''}
							>
								{#if loading}
									<svg class="size-4 animate-spin" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
										<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" />
										<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
									</svg>
								{:else}
									<Sparkles className="size-4" strokeWidth="2" />
								{/if}
								<span>{loading ? $i18n.t('Generating...') : $i18n.t('Generate')}</span>
							</button>
						</div>
					</div>
				</div>
			</section>

			<section class="workspace-section space-y-4">
				<div class="glass-item p-4 space-y-3">
					<div class="flex items-center justify-between">
						<div class="text-sm font-semibold text-gray-900 dark:text-gray-100">
							{$i18n.t('Main Prompt')}
						</div>
						<Sparkles className="size-4 text-gray-400" />
					</div>

					<textarea
						rows="5"
						bind:value={prompt}
						on:keydown={handleComposerKeydown}
						placeholder={$i18n.t('Describe the image you want to generate...')}
						class="min-h-[8rem] w-full resize-none rounded-xl border border-gray-200/60 bg-white/85 p-3 text-sm leading-6 text-gray-900 outline-none placeholder:text-gray-400 dark:border-gray-700/50 dark:bg-gray-900/70 dark:text-gray-100 dark:placeholder:text-gray-500"
					/>

					<div class="flex flex-wrap gap-1.5">
						{#each promptIdeas as idea}
							<button
								type="button"
								class="rounded-full border border-gray-200/60 bg-white/85 px-2.5 py-1 text-xs text-gray-600 transition hover:bg-gray-50 dark:border-gray-700/50 dark:bg-gray-900/70 dark:text-gray-400 dark:hover:bg-gray-800"
								on:click={() => applyPromptIdea(idea)}
							>
								{$i18n.t(idea)}
							</button>
						{/each}
					</div>
				</div>

				<div class="glass-item p-4 space-y-4">
					<div class="text-sm font-semibold text-gray-900 dark:text-gray-100">
						{$i18n.t('Generation Settings')}
					</div>

					<div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
						<div class="space-y-1.5">
							<div class="text-xs font-medium text-gray-500 dark:text-gray-400">
								{$i18n.t('Model')}
							</div>
							<div class="flex min-h-10 items-center rounded-xl border border-gray-200/60 bg-white/85 px-3 text-sm text-gray-700 dark:border-gray-700/50 dark:bg-gray-900/70 dark:text-gray-200">
								{selectedModelLabel || $i18n.t('No models available')}
							</div>
						</div>

						<div class="space-y-1.5">
							<div class="text-xs font-medium text-gray-500 dark:text-gray-400">
								{$i18n.t('Size')}
							</div>
							<HaloSelect
								bind:value={selectedSize}
								options={sizeOptions.map((o) => ({ value: o.value, label: `${o.ratio} · ${o.label}` }))}
								className="w-full text-xs"
							/>
							{#if selectedModelMeta?.size_mode === 'aspect_ratio' && selectedAspectRatio}
								<div class="text-xs text-amber-600 dark:text-amber-400">
									{$i18n.t(
										'This model only accepts aspect ratio requests. {{size}} will be sent as {{ratio}}, not as an exact pixel size.',
										{ size: selectedSize, ratio: selectedAspectRatio }
									)}
								</div>
							{/if}
						</div>

						<div class="space-y-1.5">
							<div class="flex items-center justify-between">
								<div class="text-xs font-medium text-gray-500 dark:text-gray-400">
									{$i18n.t('Set Steps')}
								</div>
								<div class="text-xs font-semibold text-gray-900 dark:text-gray-100">
									{steps === 0 ? $i18n.t('Auto') : steps}
								</div>
							</div>
							<input
								type="range"
								min="0"
								max="80"
								step="5"
								bind:value={steps}
								class="image-range mt-1 h-2 w-full cursor-pointer appearance-none rounded-full bg-gray-200 dark:bg-gray-800"
							/>
						</div>
					</div>
				</div>
			</section>

			<section bind:this={resultsSectionElement} class="workspace-section space-y-3">
				<div class="flex items-center justify-between">
					<div class="min-w-0 flex-1">
						<div class="text-sm font-semibold text-gray-900 dark:text-gray-100">
							{$i18n.t('Recent Result')}
						</div>
						{#if lastPrompt}
							<div class="mt-0.5 text-xs text-gray-500 dark:text-gray-400 truncate">
								{lastPrompt}
							</div>
						{/if}
					</div>
					{#if lastPrompt}
						<button type="button" class="workspace-icon-button" on:click={copyPromptHandler}>
							<Clipboard className="size-3.5" />
							<span class="text-xs">{$i18n.t('Copy prompt')}</span>
						</button>
					{/if}
				</div>

				{#if loading}
					<div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
						<div class="shimmer h-56 rounded-xl glass-item" />
					</div>
				{:else if generatedImages.length > 0}
					<div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
						{#each generatedImages as image, index}
							<div
								role="button"
								tabindex="0"
								class="result-card glass-item group overflow-hidden cursor-pointer p-1.5"
								style={`animation-delay: ${index * 60}ms;`}
								on:click={() => openPreview(image, index)}
								on:keydown={(event) => {
									if (event.key === 'Enter' || event.key === ' ') {
										event.preventDefault();
										openPreview(image, index);
									}
								}}
							>
								<div class="relative overflow-hidden rounded-lg bg-gray-100 dark:bg-gray-950">
									<img
										src={image.url}
										alt={`${$i18n.t('Generated image')} ${index + 1}`}
										class="max-h-[22rem] min-h-[12rem] w-full object-cover transition duration-500 group-hover:scale-[1.02]"
										loading="lazy"
									/>
									<div
										class="absolute inset-x-0 bottom-0 flex items-end justify-between gap-2 bg-gradient-to-t from-black/70 via-black/20 to-transparent p-3 text-white opacity-0 transition duration-200 group-hover:opacity-100"
									>
										<div class="text-xs font-medium">{selectedSize}</div>
										<div class="flex items-center gap-2">
											<button
												type="button"
												class="rounded-xl bg-white/15 p-2 backdrop-blur transition hover:bg-white/25"
												on:click|stopPropagation={() => openPreview(image, index)}
												aria-label={$i18n.t('Open preview')}
											>
												<ArrowsPointingOut className="size-4" />
											</button>
											<button
												type="button"
												class="rounded-xl bg-white/15 p-2 backdrop-blur transition hover:bg-white/25"
												on:click|stopPropagation={() => downloadImage(image.url, index)}
												aria-label={$i18n.t('Save image')}
											>
												<ArrowDownTray className="size-4" />
											</button>
										</div>
									</div>
								</div>
							</div>
						{/each}
					</div>
				{:else}
					<div class="workspace-empty-state">
						<div class="flex size-14 mx-auto items-center justify-center rounded-2xl bg-gray-100 text-gray-400 dark:bg-gray-800 dark:text-gray-500">
							<PhotoSolid className="size-7" />
						</div>
						<div class="mt-4 text-base font-semibold text-gray-900 dark:text-gray-100">
							{$i18n.t('Your images will appear here after generation.')}
						</div>
						<div class="mt-2 max-w-sm mx-auto text-sm leading-6 text-gray-500 dark:text-gray-400">
							{$i18n.t('Start with a strong subject, then add lighting, composition, materials, and mood for better results.')}
						</div>
					</div>
				{/if}
			</section>
		</form>
	{/if}
{/if}

<ImagePreview
	bind:show={previewOpen}
	src={previewSrc}
	alt={previewAlt}
/>
