<script lang="ts">
	import { toStore } from 'svelte/store';
	import { goto } from '$app/navigation';
	import { createQuery, useQueryClient } from '@tanstack/svelte-query';
	import { assetUrl, canvasApi, jobsApi, productionApi, projects, type ProductionCamera, type Clip, type Job } from '$lib/api';
	import JobRow from '$lib/components/JobRow.svelte';
	import StoryboardPanel from '$lib/components/StoryboardPanel.svelte';
	import ProductionVideoPanel from '$lib/components/ProductionVideoPanel.svelte';
	import Button from '$lib/components/ui/Button.svelte';

	let { projectId }: { projectId: number } = $props();
	const client = useQueryClient();
	const production = createQuery(toStore(() => ({
		queryKey: ['production', projectId], queryFn: () => productionApi.get(projectId),
	})));
	const scenes = createQuery(toStore(() => ({
		queryKey: ['scenes', projectId], queryFn: () => projects.getScenes(projectId),
	})));
	const jobs = createQuery(toStore(() => ({
		queryKey: ['jobs', projectId], queryFn: () => jobsApi.list(projectId), refetchInterval: 3000,
	})));
	const previsJobs = $derived(($jobs.data ?? []).filter(j => j.kind === 'previs'));
	let queued = $state('');
	async function render(mode: 'stills' | 'animation') {
		if (!$production.data) return;
		error = ''; queued = '';
		try {
			const ids = selected === null ? $production.data.shots.map(s => s.clip_id) : [selected];
			const job = await productionApi.render(projectId, $production.data.revision, ids, mode);
			queued = `Previs job ${job.id} queued from saved revision ${$production.data.revision}.`;
			await $jobs.refetch();
		} catch (e) { error = e instanceof Error ? e.message : 'Could not queue previs'; }
	}
	function previews(job: Job) {
		const paths = job.output_paths.filter(p => selected === null ||
			p.includes(`shot-${selected}-`) || p.endsWith(`shot-${selected}.mp4`));
		const videos = paths.filter(p => p.endsWith('.mp4'));
		return videos.length ? videos : paths.filter(p => p.endsWith('.png'));
	}
	function isStale(job: Job) {
		const hashes = job.payload.shot_hashes as Record<string, string> | undefined;
		const ids = job.payload.clip_ids as number[] | undefined;
		return (ids ?? []).filter(id => selected === null || id === selected).some(id =>
			hashes?.[String(id)] !== $production.data?.shots.find(s => s.clip_id === id)?.source_hash);
	}
	let selected = $state<number | null>(null);
	let camera = $state<ProductionCamera | null>(null);
	let endCamera = $state<ProductionCamera | null>(null);
	let frameEnd = $state(240);
	let roughBoardId = $state('');
	let editRevision = $state(0);
	let saving = $state(false);
	let error = $state('');
	const objects = $derived($production.data?.world.objects ?? []);
	const extent = $derived(Math.max(10,
		...objects.flatMap(o => [Math.abs(o.position[0]) + o.scale[0] * 2 + 2,
			Math.abs(o.position[1]) + o.scale[1] * 2 + 2]),
		...(camera ? [Math.abs(camera.position[0]) + 2, Math.abs(camera.position[1]) + 2,
			Math.abs(camera.target[0]) + 2, Math.abs(camera.target[1]) + 2] : [])));

	function selectShot(clip: Clip) {
		if (!$production.data) return;
		const shot = $production.data.shots.find(s => s.clip_id === clip.id);
		selected = clip.id;
		camera = structuredClone(shot?.camera ?? {
			position: [6, -8, 5], target: [0, 0, 1], lens_mm: 40, sensor_width_mm: 36,
		});
		endCamera = shot?.end_camera ? structuredClone(shot.end_camera) : null;
		frameEnd = shot?.frame_end ?? Math.max(1, Math.round((clip.duration_sec ?? 10) * 24));
		roughBoardId = shot?.rough_board_id ?? $production.data.selected_boards[`${clip.id}:rough`] ?? '';
		editRevision = $production.data.revision;
		error = '';
	}
	async function saveCamera() {
		if (!camera || selected === null) return;
		saving = true;
		error = '';
		try {
			const updated = await productionApi.setCamera(projectId, selected, {
				expected_revision: editRevision, camera, end_camera: endCamera, frame_end: frameEnd,
				rough_board_id: roughBoardId,
			});
			client.setQueryData(['production', projectId], updated);
			editRevision = updated.revision;
		} catch (e) { error = e instanceof Error ? e.message : 'Could not save the camera'; }
		finally { saving = false; }
	}
	async function refresh() {
		await Promise.all([$production.refetch(), $scenes.refetch()]);
		selected = null; camera = null; error = '';
	}
	async function openChat() {
		try {
			const canvas = await canvasApi.ensureForProject(projectId);
			await goto(`/canvas/${canvas.canvas.id}`);
		} catch (e) { error = e instanceof Error ? e.message : 'Could not open project chat'; }
	}
</script>

<section class="production">
	<header>
		<div><p class="eyebrow">SHARED WORLD · 24 FPS</p><h1>Camera blocking</h1>
			<p class="intro">Plan every shot in the same space. Camera and world edits retain earlier revisions.</p></div>
		<div class="actions"><Button variant="secondary" onclick={refresh}>Reload</Button>
			<Button onclick={openChat}>Direct in project chat</Button></div>
	</header>
	{#if error}<p class="error" role="alert">{error}</p>{/if}
	{#if queued}<p role="status">{queued}</p>{/if}
	{#if $production.isLoading || $scenes.isLoading}
		<p>Loading production…</p>
	{:else if $production.isError || $scenes.isError}
		<p class="error" role="alert">{$production.error?.message ?? $scenes.error?.message}</p>
	{:else if $production.data && $scenes.data}
		<div class="layout">
			<aside class="shots" aria-label="Production shots">
				<h2>Shot list</h2>
				{#each $scenes.data.scenes as scene (scene.id)}
					<h3>{scene.heading ?? `Scene ${scene.order_index}`}</h3>
					{#each scene.clips as clip (clip.id)}
						<button class:chosen={selected === clip.id} onclick={() => selectShot(clip)}>
							<strong>{clip.label ?? `Shot ${scene.order_index}.${clip.order_index}`}</strong>
							<span>{clip.description ?? scene.action ?? 'Camera not directed yet'}</span>
							<small>{$production.data.shots.some(s => s.clip_id === clip.id) ? 'Camera saved' : 'Set camera'}</small>
						</button>
					{/each}
				{:else}<p class="intro">Plan a scene and its shots in the Script stage or project chat first.</p>{/each}
			</aside>
			<div class="world">
				<div class="view-label"><strong>World plan</strong><span>Top view · meters · revision {$production.data.revision}</span></div>
				<svg viewBox={`${-extent} ${-extent} ${extent * 2} ${extent * 2}`} role="img" aria-label="Top view of the shared world and selected camera">
					<g transform="scale(1,-1)">
						<path d={`M ${-extent} 0 H ${extent} M 0 ${-extent} V ${extent}`} stroke="#3a3944" stroke-width="0.04" stroke-dasharray="0.2 0.2" />
						{#each objects as object (object.id)}
							<g transform={`translate(${object.position[0]},${object.position[1]}) rotate(${object.rotation[2] * 180 / Math.PI})`}>
								<title>{object.id} · {object.position.join(', ')} m</title>
								{#if object.kind === 'sphere' || object.kind === 'cylinder'}
									<ellipse rx={object.scale[0]} ry={object.scale[1]} fill={`rgb(${object.color.map(c => c * 255).join(',')})`} opacity="0.75" />
								{:else}<rect x={-object.scale[0]} y={-object.scale[1]} width={object.scale[0] * 2} height={object.scale[1] * 2} fill={`rgb(${object.color.map(c => c * 255).join(',')})`} opacity="0.75" />{/if}
							</g>
						{/each}
						{#if camera}
							<line x1={camera.position[0]} y1={camera.position[1]} x2={camera.target[0]} y2={camera.target[1]} stroke="#c8b5ff" stroke-width="0.08" />
							<circle cx={camera.position[0]} cy={camera.position[1]} r="0.23" fill="#c8b5ff" />
							<circle cx={camera.target[0]} cy={camera.target[1]} r="0.16" fill="none" stroke="#c8b5ff" stroke-width="0.06" />
						{/if}
					</g>
				</svg>
				<p class="intro">{objects.length ? `${objects.length} objects share this world.` : 'Direct a location in project chat to build the shared world.'}</p>
			</div>
			<aside class="camera">
				<h2>Camera</h2>
				{#if camera}
					<form onsubmit={(event) => { event.preventDefault(); saveCamera(); }}>
						<label class="field">Rough board for Blender<select class="field-input" bind:value={roughBoardId}><option value="">No board reference</option>{#each $production.data.boards.filter(b => b.clip_id === selected && b.stage === 'rough') as board}<option value={board.id}>{board.id.slice(0,8)}</option>{/each}</select></label>
						{#each ['position', 'target'] as group}
							<fieldset><legend>{group === 'position' ? 'Position (m)' : 'Look at (m)'}</legend>
								<div class="coordinates">{#each ['X', 'Y', 'Z'] as axis, index}
									<label>{axis}<input class="field-input" type="number" step="0.1" required bind:value={camera[group as 'position' | 'target'][index]} /></label>
								{/each}</div>
							</fieldset>
						{/each}
						<label class="field">Lens (mm)<input class="field-input" type="number" min="10" max="300" required bind:value={camera.lens_mm} /></label>
						<label class="field">Frames at 24 fps<input class="field-input" type="number" min="1" max="172800" required bind:value={frameEnd} /></label>
						<label class="move-toggle"><input type="checkbox" checked={endCamera !== null}
							onchange={() => { endCamera = endCamera ? null : structuredClone(camera); }} />Camera move</label>
						{#if endCamera}
							{#each ['position', 'target'] as group}
								<fieldset><legend>{group === 'position' ? 'Destination position (m)' : 'Destination look at (m)'}</legend>
									<div class="coordinates">{#each ['X', 'Y', 'Z'] as axis, index}
										<label>{axis}<input class="field-input" type="number" step="0.1" required
											bind:value={endCamera[group as 'position' | 'target'][index]} /></label>
									{/each}</div>
								</fieldset>
							{/each}
							<label class="field">Destination lens (mm)<input class="field-input" type="number" min="10" max="300" required bind:value={endCamera.lens_mm} /></label>
						{/if}
						<Button type="submit" disabled={saving}>{saving ? 'Saving…' : 'Save camera'}</Button>
					</form>
				{:else}<p class="intro">Select a shot to place its camera.</p>{/if}
			</aside>
		</div>
		{#if selected !== null}
			{#key selected}<StoryboardPanel {projectId} clipId={selected} productionState={$production.data} jobs={$jobs.data ?? []}
				onUpdate={(state) => client.setQueryData(['production',projectId],state)} onQueued={() => $jobs.refetch()} />{/key}
			{#key selected}<ProductionVideoPanel {projectId} clipId={selected} productionState={$production.data} jobs={$jobs.data ?? []} onQueued={() => $jobs.refetch()} onUpdate={(state) => {client.setQueryData(['production',projectId],state); client.invalidateQueries({queryKey:['scenes',projectId]});}} />{/key}
		{/if}
		<section class="renders" aria-label="Blender renders">
			<div class="view-label"><h2>Blender previs</h2><div class="actions">
				<Button variant="secondary" onclick={() => render('stills')} disabled={!$production.data.shots.length}>Render saved views</Button>
				<Button onclick={() => render('animation')} disabled={!$production.data.shots.length}>Render moving previs</Button>
			</div></div>
			<p class="intro">Renders use saved cameras{selected === null ? ' for all shots' : ' for the selected shot'}. Earlier takes stay available after camera edits.</p>
			{#each previsJobs as job (job.id)}
				<JobRow {job} label={`Blender · revision ${(job.payload.document as {revision?: number})?.revision ?? '?'}`} />
				{#if job.status === 'done'}
					<p class="intro">{isStale(job) ? 'Earlier geometry or camera revision' : 'Matches saved camera and world'}
						{#each job.output_paths.filter(p => p.endsWith('.blend')) as path} · <a href={assetUrl(path) ?? ''}>Shared Blender scene</a>{/each}</p>
					<div class="previews">{#each previews(job) as path}
						{#if path.endsWith('.mp4')}<video src={assetUrl(path) ?? ''} controls muted aria-label="Moving Blender previs"></video>
						{:else}<img src={assetUrl(path) ?? ''} alt="Rendered Blender camera view" loading="lazy" />{/if}
					{/each}</div>
				{/if}
			{/each}
		</section>
	{/if}
</section>

<style>
	.production { max-width: 1600px; margin: 0 auto; }
	.renders { margin-top: 24px; padding: 20px; border: 1px solid var(--border); border-radius: 12px; }
	.previews { display: grid; grid-template-columns: repeat(auto-fit,minmax(260px,1fr)); gap: 12px; margin: 16px 0; }
	.previews img, .previews video { width: 100%; border-radius: 8px; background: #121219; }
	.move-toggle { display: flex; align-items: center; gap: 8px; margin: 16px 0; font-size: 12px; }
	.move-toggle input { width: auto; }
	header, .actions, .view-label { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
	header { margin-bottom: 24px; flex-wrap: wrap; }
	h1 { font-size: 28px; margin: 4px 0 8px; } h2 { font-size: 15px; margin: 0 0 16px; }
	h3 { font-size: 12px; color: var(--text-secondary); margin: 22px 0 10px; }
	.eyebrow { color: var(--accent); font-size: 10px; letter-spacing: 0.14em; margin: 0; }
	.intro { color: var(--text-secondary); font-size: 13px; line-height: 1.5; }
	.layout { display: grid; grid-template-columns: 210px minmax(240px, 1fr) 230px; gap: 16px; }
	.shots, .camera, .world { background: var(--bg-surface); border: 1px solid var(--border); border-radius: 12px; padding: 18px; min-width: 0; }
	.shots button { width: 100%; text-align: left; padding: 12px; background: var(--bg-elevated); border: 1px solid var(--border); border-radius: 8px; color: var(--text-primary); cursor: pointer; margin-bottom: 8px; }
	.shots button.chosen { border-color: var(--accent); background: rgba(139,92,246,0.12); }
	.shots button span, .shots button small { display: block; margin-top: 6px; font-size: 12px; line-height: 1.5; color: var(--text-secondary); }
	.shots button small { color: var(--accent); font-size: 10px; }
	.view-label { font-size: 12px; flex-wrap: wrap; } .view-label span { color: var(--text-secondary); font-size: 10px; }
	svg { display: block; width: 100%; aspect-ratio: 1.2; margin: 16px 0; background: #121219; border-radius: 8px; }
	fieldset { border: 0; padding: 0; margin: 0 0 16px; } legend { font-size: 12px; color: var(--text-secondary); margin-bottom: 10px; }
	.coordinates { display: grid; grid-template-columns: repeat(3,1fr); gap: 8px; }
	.coordinates label { font-size: 10px; color: var(--text-secondary); } input { min-width: 0; width: 100%; }
	.coordinates input { padding: 8px 6px; font-size: 12px; }
	.error { padding: 12px; color: var(--error); border: 1px solid var(--error); border-radius: 8px; }
	@media(max-width: 1100px) { .layout { grid-template-columns: 180px minmax(200px,1fr); } .camera { grid-column: 1/-1; } }
	@media(max-width: 700px) { .layout { grid-template-columns: 1fr; } }
</style>
