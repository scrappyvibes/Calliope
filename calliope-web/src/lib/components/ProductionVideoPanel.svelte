<script lang="ts">
	import { createQuery } from '@tanstack/svelte-query';
	import { assetUrl, productionApi, workflows, type Job, type ProductionState } from '$lib/api';
	import ComfyDynamicForm from './ComfyDynamicForm.svelte';
	import JobRow from './JobRow.svelte';
	import Button from './ui/Button.svelte';
	let { projectId, clipId, productionState, jobs, onQueued, onUpdate }: {
		projectId: number; clipId: number; productionState: ProductionState; jobs: Job[]; onQueued: () => void;
		onUpdate: (state: ProductionState) => void;
	} = $props();
	const library = createQuery({queryKey:['workflows'], queryFn:workflows.list});
	let workflowId = $state(0);
	let boardId = $state('');
	let previsId = $state(0);
	let prompt = $state('');
	let duration = $state(5);
	let seed = $state(1);
	let extra = $state<Record<string,string|number>>({});
	let busy = $state(false);
	let error = $state('');
	let notice = $state('');
	const choices = $derived(($library.data ?? []).filter(w => w.kind === 'video' && w.is_enabled &&
		Object.values(w.workflow_json).some((node) => {
			const n = node as {_meta?: {calliope_adapter?: {name?: string}}};
			return n._meta?.calliope_adapter?.name === 'scrappyvibes_h3_references';
		})));
	const workflow = $derived(choices.find(w => w.id === workflowId));
	const boards = $derived(productionState.boards.filter(b => b.clip_id === clipId && b.stage === 'styled' && !b.stale));
	const shotHash = $derived(productionState.shots.find(s => s.clip_id === clipId)?.source_hash);
	const motions = $derived(jobs.filter(j => j.kind === 'previs' && j.status === 'done' &&
		(j.payload.shot_hashes as Record<string,string> | undefined)?.[String(clipId)] === shotHash &&
		j.output_paths.some(p => p.endsWith(`shot-${clipId}.mp4`))));
	const needsMotion = $derived(workflow?.input_schema.some(i => i.role === 'video_1') ?? false);
	const extraInputs = $derived((workflow?.input_schema ?? []).filter(i =>
		['image','video','audio'].includes(i.kind) && !['image_1','video_1'].includes(i.role ?? '')));
	const frames = $derived(Math.round(duration * 24) + ((5 - Math.round(duration * 24) % 17) + 17) % 17);
	const takes = $derived(jobs.filter(j => (j.payload.production_video_target as {clip_id?:number} | undefined)?.clip_id === clipId));
	$effect(() => {
		if (!workflowId && choices.length) workflowId = choices[0].id;
		if (!boardId) boardId = productionState.selected_boards[`${clipId}:styled`] ?? boards[0]?.id ?? '';
		if (!previsId && motions.length) previsId = motions[0].id;
	});
	async function generate() {
		busy = true; error = ''; notice = '';
		try {
			const job = await productionApi.generateVideo(projectId, {
				expected_revision:productionState.revision, clip_id:clipId, workflow_id:workflowId,
				styled_board_id:boardId, previs_job_id:needsMotion ? previsId : null,
				prompt, duration_seconds:duration, seed,
				reference_paths:Object.fromEntries(Object.entries(extra).map(([k,v]) => [k,String(v)])),
			});
			notice = `H3 job ${job.id} queued with saved references. Review the candidate when it finishes.`;
			onQueued();
		} catch(e) { error = e instanceof Error ? e.message : 'Could not queue H3'; }
		finally { busy = false; }
	}
	async function select(job: Job, index: number) {
		busy = true; error = '';
		try { onUpdate(await productionApi.selectVideo(projectId, productionState.revision, job.id, index)); }
		catch(e) { error = e instanceof Error ? e.message : 'Could not select video'; }
		finally { busy = false; }
	}
	function revise(job: Job) {
		const target = job.payload.production_video_target as {
			prompt:string; seed:number; requested_duration_seconds:number;
			styled_board_id:string; previs_job_id:number|null;
		};
		workflowId = job.workflow_id ?? 0; prompt = target.prompt; seed = target.seed;
		duration = target.requested_duration_seconds; boardId = target.styled_board_id;
		previsId = target.previs_job_id ?? 0;
		const refs = job.payload.reference_manifest as Array<{node_id:string;label:string;path:string}>;
		extra = Object.fromEntries(refs.filter(r => !['Picture 1','Video 1'].includes(r.label)).map(r => [r.node_id,r.path]));
		notice = `Loaded job ${job.id} settings for revision. Edit the prompt or references above, then generate a separate candidate.`;
	}
</script>

<section class="video-production" aria-label="Shot H3 generation">
	<header><p class="eyebrow">SHOT {clipId} · WORKFLOW B</p><h2>Animate the styled scene</h2>
		<p class="muted">Use the styled board for appearance and Blender for camera movement. Each attempt keeps its own prompt, seed and references.</p></header>
	<div class="fields">
		<label class="field"><span class="field-label">H3 workflow</span><select class="field-input" bind:value={workflowId} onchange={() => extra = {}}><option value={0}>Choose workflow</option>{#each choices as w}<option value={w.id}>{w.name}</option>{/each}</select></label>
		<label class="field"><span class="field-label">Picture 1 · styled board</span><select class="field-input" bind:value={boardId}><option value="">Choose current styled board</option>{#each boards as b}<option value={b.id}>{b.id.slice(0,8)}{productionState.selected_boards[`${clipId}:styled`] === b.id ? ' · selected' : ''}</option>{/each}</select></label>
		{#if needsMotion}<label class="field"><span class="field-label">Video 1 · Blender motion</span><select class="field-input" bind:value={previsId}><option value={0}>Choose current motion reference</option>{#each motions as j}<option value={j.id}>Blender job {j.id}</option>{/each}</select></label>{/if}
	</div>
	{#if boards.find(b => b.id === boardId)}<img class="reference" src={assetUrl(boards.find(b => b.id === boardId)!.path)} alt="Styled appearance reference" />{/if}
	<label class="field"><span class="field-label">H3 prompt</span><textarea class="field-textarea" rows="12" bind:value={prompt} placeholder="Define subjects, appearance from <Picture 1>, camera movement from <Video 1>, the timed action, soundscape and music."></textarea></label>
	<div class="fields">
		<label class="field"><span class="field-label">Requested seconds</span><input class="field-input" type="number" min="5" max="15" step="0.1" bind:value={duration} /></label>
		<label class="field"><span class="field-label">Seed</span><input class="field-input" type="number" min="0" max={Number.MAX_SAFE_INTEGER} step="1" bind:value={seed} /></label>
	</div>
	<p class="muted">Output: {frames} frames at 24 fps · {(frames / 24).toFixed(3)} seconds on H3’s frame grid.</p>
	{#if extraInputs.length}<ComfyDynamicForm inputs={extraInputs} bind:values={extra} assetOptions={jobs.flatMap(j => j.output_paths.map(path => ({label:`Job ${j.id} · ${path.split(/[\\/]/).pop()}`,path})))} />{/if}
	{#if error}<p role="alert">{error}</p>{/if}
	{#if notice}<p role="status">{notice}</p>{/if}
	<Button onclick={generate} disabled={busy || !workflowId || !boardId || !prompt.trim() || (needsMotion && !previsId)}>Generate H3 candidate</Button>
	<div class="takes">{#each takes as job}<article><JobRow {job} />
		<Button variant="secondary" onclick={() => revise(job)} disabled={busy}>Revise this attempt</Button>
		<details><summary>Saved prompt and references</summary>
			<p>Seed {(job.payload.production_video_target as {seed:number}).seed} · {(job.payload.production_video_target as {frame_count:number}).frame_count} frames · source revision {(job.payload.production_video_target as {production_revision:number}).production_revision}</p>
			<pre>{(job.payload.production_video_target as {prompt:string}).prompt}</pre>
			<ul>{#each (job.payload.reference_manifest as Array<{label:string;sha256:string}>) as ref}<li>{ref.label} · SHA-256 <code>{ref.sha256}</code></li>{/each}</ul>
		</details>
		{#if (job.payload.production_video_target as {source_shot_hash?:string})?.source_shot_hash !== shotHash}<p>Earlier camera or world</p>{/if}
		{#each job.output_paths as path,index}{#if /\.(mp4|webm|mov)$/i.test(path)}
			<video aria-label={`H3 job ${job.id} output ${index + 1}`} controls preload="metadata" src={assetUrl(path)}><track kind="captions" /></video>
			<Button variant="secondary" disabled={busy || (productionState.selected_videos ?? {})[String(clipId)] === `${job.id}:${index}` || (job.payload.production_video_target as {source_shot_hash?:string})?.source_shot_hash !== shotHash} onclick={() => select(job,index)}>{(productionState.selected_videos ?? {})[String(clipId)] === `${job.id}:${index}` ? 'Selected take' : 'Select reviewed take'}</Button>
		{/if}{/each}
	</article>{/each}</div>
</section>

<style>
	.video-production { margin-top: 2rem; padding-top: 1.5rem; border-top: 1px solid var(--border); }
	.fields { display: grid; grid-template-columns: repeat(auto-fit,minmax(180px,1fr)); gap: 1rem; margin: 1rem 0; }
	.reference { width: min(100%, 400px); max-height: 240px; object-fit: contain; margin-bottom: 1rem; border-radius: 8px; }
	.takes { display: grid; gap: 1rem; margin-top: 1.5rem; }
	.takes video { width: min(100%, 680px); display: block; margin-top: .75rem; }
	pre { white-space: pre-wrap; overflow-wrap: anywhere; font: inherit; line-height: 1.6; }
	code { overflow-wrap: anywhere; }
	.eyebrow { font-size: .7rem; letter-spacing: .15em; color: var(--text-secondary); }
</style>
