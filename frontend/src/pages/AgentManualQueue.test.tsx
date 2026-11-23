// @vitest-environment jsdom
import {afterEach, beforeAll, describe, expect, it, vi} from 'vitest';
import {cleanup, fireEvent, render, screen, waitFor} from '@testing-library/react';
import {MemoryRouter} from 'react-router-dom';
import {DraftCard} from './Agents';

beforeAll(()=>{
 Object.defineProperty(globalThis,'ResizeObserver',{writable:true,value:class {observe(){} unobserve(){} disconnect(){}}});
 Object.defineProperty(window,'matchMedia',{writable:true,value:vi.fn(query=>({matches:false,media:query,onchange:null,addListener:vi.fn(),removeListener:vi.fn(),addEventListener:vi.fn(),removeEventListener:vi.fn(),dispatchEvent:vi.fn()}))});
 const style=window.getComputedStyle;vi.spyOn(window,'getComputedStyle').mockImplementation(element=>style(element));
});
afterEach(cleanup);

const agent={id:'simon',name:'Simon',account_id:6503} as any;
const manualPost={id:'d-1',agent_id:'simon',account_id:6503,action:'post',status:'draft',body:'Previewed text',origin:'manual'} as any;

function setup(draft:any,handlers:Record<string,any>={}) {
 const props={onPublish:vi.fn(async()=>true),onDiscard:vi.fn(async()=>true),onReconcile:vi.fn(async()=>true),onQueue:vi.fn(async()=>true),onCancelQueue:vi.fn(async()=>true),...handlers};
 render(<MemoryRouter><DraftCard draft={draft} agent={agent} busy={false} {...props}/></MemoryRouter>);
 return props;
}

describe('manual publish queue on the draft card',()=>{
 it('queues the previewed body of a manual post',async()=>{
  const props=setup(manualPost);
  fireEvent.click(screen.getByRole('button',{name:'排队发布'}));
  await waitFor(()=>expect(props.onQueue).toHaveBeenCalledWith('d-1','Previewed text'));
  expect(screen.getByRole('button',{name:'发 布'})).toBeTruthy();
 });

 it('cannot queue an edited body, because the queue only sends what was previewed',()=>{
  const props=setup(manualPost);
  fireEvent.change(screen.getByRole('textbox'),{target:{value:'Changed text'}});
  const queue=screen.getByRole('button',{name:'排队发布'}) as HTMLButtonElement;
  expect(queue.disabled).toBe(true);
  fireEvent.click(queue);
  expect(props.onQueue).not.toHaveBeenCalled();
 });

 it('shows the waiting state with cancel instead of publish',async()=>{
  const props=setup({...manualPost,manual_queue:{state:'waiting',attempts:2,expires_at:1761100000}});
  expect(screen.getByLabelText('排队状态').textContent).toMatch(/^排队中：有发布名额时自动发送一次 · 已尝试 2 次 · .+ 前没发出就停止排队$/);
  expect(screen.queryByRole('button',{name:'发 布'})).toBeNull();
  expect(screen.queryByRole('button',{name:'排队发布'})).toBeNull();
  expect((screen.getByRole('textbox') as HTMLTextAreaElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole('button',{name:'取消排队'}));
  await waitFor(()=>expect(props.onCancelQueue).toHaveBeenCalledWith('d-1'));
 });

 it('reports how a finished queue ended and lets the member publish again',()=>{
  setup({...manualPost,manual_queue:{state:'expired',attempts:5}});
  expect(screen.getByLabelText('排队状态').textContent).toBe('排队超时，没有发送，草稿保留为待确认');
  expect(screen.getByRole('button',{name:'排队发布'})).toBeTruthy();
 });

 it('offers no queue for automatic drafts or actions the queue does not take',()=>{
  setup({...manualPost,origin:undefined});
  expect(screen.queryByRole('button',{name:'排队发布'})).toBeNull();
  cleanup();
  setup({...manualPost,action:'like',body:'',target_post_id:5});
  expect(screen.queryByRole('button',{name:'排队发布'})).toBeNull();
 });
});
