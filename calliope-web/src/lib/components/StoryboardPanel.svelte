<script lang="ts">
	import { createQuery } from '@tanstack/svelte-query';
	import { assetUrl, productionApi, workflows, type BoardSource, type Job, type ProductionState } from '$lib/api';
	import ComfyDynamicForm from './ComfyDynamicForm.svelte';
	import JobRow from './JobRow.svelte';
	import Button from './ui/Button.svelte';
	let { projectId, clipId, productionState, jobs, onUpdate, onQueued }: {
		projectId: number; clipId: number; productionState: ProductionState; jobs: Job[];
		onUpdate: (state: ProductionState) => void; onQueued: () => void;
	} = $props();
	const library = createQuery({queryKey:['workflows'], queryFn:workflows.list});
	let stage = $state<BoardSource['stage']>('rough');
	let sourceBoard = $state('');
	let sourceFrame = $state('');
	let workflowId = $state(0);
	let values = $state<Record<string,string|number>>({});
	let busy = $state(false);
	let error = $state('');
	let notice = $state('');
	const workflow = $derived($library.data?.find(w => w.id === workflowId));
	const boards = $derived((productionState.boards ?? []).filter(b => b.clip_id === clipId));
	const sourceOptions = $derived(boards.filter(b => b.stage === (stage === 'styled' ? 'refined' : 'rough')));
	const frames = $derived(jobs.filter(j => j.kind === 'previs' && j.status === 'done').flatMap(j =>
		j.output_paths.flatMap(p => {
			const match = p.match(new RegExp(`shot-${clipId}-(\\d+)\\.png$`));
			return match ? [{key:`${j.id}:${Number(match[1])}`, label:`Blender job ${j.id} · frame ${Number(match[1])}`, path:p}] : [];
		})));
	const generations = $derived(jobs.filter(j => {
		const target = j.payload.storyboard_target as {clip_id?: number; stage?: string} | undefined;
		return target?.clip_id === clipId && target.stage === stage;
	}));
	function source(): BoardSource {
		const [job, frame] = sourceFrame.split(':').map(Number);
		return {expected_revision:productionState.revision, clip_id:clipId, stage,
			source_board_id:sourceBoard || null,
			previs_job_id:stage === 'refined' ? job || null : null,
			previs_frame:stage === 'refined' ? frame || null : null};
	}
	function chooseWorkflow() {
		values = Object.fromEntries((workflow?.input_schema ?? []).filter(i =>
			i.kind !== 'image' && i.defaultValue !== undefined && i.defaultValue !== null)
			.map(i => [i.nodeId, i.defaultValue as string | number]));
	}
	async function run(action: () => Promise<void>) {
		busy = true; error = ''; notice = '';
		try { await action(); } catch(e) { error = e instanceof Error ? e.message : 'Storyboard operation failed'; }
		finally { busy = false; }
	}
	async function upload(event: Event) {
		const input = event.currentTarget as HTMLInputElement;
		const file = input.files?.[0]; if (!file) return;
		await run(async () => { onUpdate(await productionApi.uploadBoard(projectId, source(), file)); notice = 'Board saved as a candidate.'; });
		input.value = '';
	}
	async function generate() {
		await run(async () => {
			const job = await productionApi.generateBoard(projectId, {...source(), workflow_id:workflowId, input_values:values});
			onQueued(); notice = `Image job ${job.id} queued. Review its output before selecting a take.`;
		});
	}
	async function saveOutput(job: Job, output_index: number) {
		const target = job.payload.storyboard_target as BoardSource;
		await run(async () => onUpdate(await productionApi.registerBoard(projectId, {
			expected_revision:productionState.revision, clip_id:clipId, stage:target.stage,
			source_board_id:target.source_board_id, previs_job_id:target.previs_job_id,
			previs_frame:target.previs_frame, generation_job_id:job.id, output_index,
		})));
	}
</script>

<section class="storyboards" aria-label="Shot storyboards">
	<header><div><p class="eyebrow">SHOT {clipId}</p><h2>Storyboard takes</h2></div>
		<div class="tabs">{#each ['rough','refined','styled'] as item}
			<button class:active={stage === item} onclick={() => { stage = item as BoardSource['stage']; sourceBoard = ''; }}>{item}</button>
		{/each}</div>
	</header>
	<p class="hint">{stage === 'rough' ? 'Explore the shot before blocking. Upload a sketch or generate with an image workflow.' : stage === 'refined' ? 'Choose the rendered camera frame that anchors this board. Upload a refined image or generate from that frame.' : 'Choose a refined board and apply Workflow A. Prompts, source links and previous takes stay recorded.'}</p>
	{#if error}<p class="error" role="alert">{error}</p>{/if}
	{#if notice}<p role="status">{notice}</p>{/if}
	<div class="sources">
		{#if stage === 'refined'}<label>Blender camera frame<select class="field-input" bind:value={sourceFrame}>
			<option value="">Choose a rendered frame</option>{#each frames as frame}<option value={frame.key}>{frame.label}</option>{/each}
		</select></label>{/if}
		<label>{stage === 'styled' ? 'Refined source board' : 'Rough source board (optional)'}<select class="field-input" bind:value={sourceBoard}>
			<option value="">Choose a source board</option>{#each sourceOptions as board}<option value={board.id}>{board.id.slice(0,8)}{board.stale ? ' · earlier camera' : ''}</option>{/each}
		</select></label>
		<label>Upload {stage} image<input type="file" accept="image/png,image/jpeg,image/webp" disabled={busy} onchange={upload} /></label>
	</div>
	{#if stage === 'refined' && sourceFrame}<img class="source-preview" src={assetUrl(frames.find(f => f.key === sourceFrame)?.path) ?? ''} alt="Source Blender camera frame" />{/if}
	<details><summary>Generate a {stage} board</summary>
		<label>Image workflow<select class="field-input" bind:value={workflowId} onchange={chooseWorkflow}>
			<option value={0}>Choose a workflow</option>{#each ($library.data ?? []).filter(w => w.kind === 'image' && w.is_enabled) as entry}<option value={entry.id}>{entry.name}</option>{/each}
		</select></label>
		{#if workflow}<ComfyDynamicForm inputs={workflow.input_schema.filter(i => !['image','audio','video'].includes(i.kind))} bind:values quiet />{/if}
		<Button disabled={busy || !workflow} onclick={generate}>Generate candidate</Button>
	</details>
	<div class="takes">{#each boards.filter(b => b.stage === stage) as board}
		<article><img src={assetUrl(board.path) ?? ''} alt={`${stage} storyboard take`} />
			<p>{board.id.slice(0,8)} · {board.stale ? 'Earlier camera/world' : 'Current source'}</p>
			<Button variant="secondary" disabled={busy || board.stale || productionState.selected_boards?.[`${clipId}:${stage}`] === board.id}
				onclick={() => run(async () => onUpdate(await productionApi.selectBoard(projectId, productionState.revision, board.id)))}>
				{productionState.selected_boards?.[`${clipId}:${stage}`] === board.id ? 'Selected take' : 'Select take'}</Button>
		</article>
	{/each}</div>
	{#each generations as job}
		<JobRow {job} label={`${stage} board · job ${job.id}`} />
		{#if job.status === 'done'}<div class="takes">{#each job.output_paths as path,index}
			<article><img src={assetUrl(path) ?? ''} alt="Generated storyboard candidate" />
				<Button variant="secondary" disabled={busy} onclick={() => saveOutput(job,index)}>Save candidate</Button></article>
		{/each}</div>{/if}
	{/each}
</section>

<style>
	.storyboards { margin-top:24px; padding:20px; border:1px solid var(--border); border-radius:12px; }
	header,.tabs { display:flex; align-items:center; justify-content:space-between; gap:10px; }
	h2 { font-size:18px; margin:4px 0; } .eyebrow { color:var(--accent); font-size:10px; letter-spacing:0.12em; }
	.tabs button { padding:9px 16px; border:1px solid var(--border); border-radius:6px; background:var(--bg-elevated); color:var(--text-secondary); text-transform:capitalize; cursor:pointer; }
	.tabs button.active { color:var(--text-primary); border-color:var(--accent); }
	.hint,article p { font-size:12px; line-height:1.6; color:var(--text-secondary); }
	.sources { display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:16px; margin:18px 0; }
	label { display:block; font-size:12px; color:var(--text-secondary); } select,input { display:block; width:100%; margin-top:8px; }
	details { margin:18px 0; padding:16px; border:1px solid var(--border); border-radius:8px; } summary { cursor:pointer; margin-bottom:12px; }
	.takes { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:16px; margin:16px 0; }
	article { border:1px solid var(--border); padding:12px; border-radius:8px; } article img { width:100%; border-radius:6px; }
	.source-preview { width:280px; max-width:100%; border-radius:8px; } .error { color:var(--error); }
</style>
