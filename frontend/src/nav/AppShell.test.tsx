// @vitest-environment jsdom
import {afterEach, beforeAll, describe, expect, it, vi} from 'vitest';
import {cleanup, fireEvent, render, screen, waitFor, within} from '@testing-library/react';
import {MemoryRouter} from 'react-router-dom';
import {QueryClient, QueryClientProvider} from '@tanstack/react-query';
import {App} from 'antd';
import AppShell from './AppShell';

beforeAll(()=>{
 Object.defineProperty(globalThis,'ResizeObserver',{writable:true,value:class {observe(){} unobserve(){} disconnect(){}}});
 Object.defineProperty(window,'matchMedia',{writable:true,value:vi.fn(query=>({matches:false,media:query,onchange:null,addListener:vi.fn(),removeListener:vi.fn(),addEventListener:vi.fn(),removeEventListener:vi.fn(),dispatchEvent:vi.fn()}))});
 const style=window.getComputedStyle;vi.spyOn(window,'getComputedStyle').mockImplementation(element=>style(element));
});
let client:QueryClient;
afterEach(()=>{cleanup();client?.clear();});

function setup(initial:string,seed?:(client:QueryClient)=>void) {
 client=new QueryClient({defaultOptions:{queries:{retry:false}}});
 seed?.(client);
 render(<QueryClientProvider client={client}><App><MemoryRouter initialEntries={[initial]}><AppShell><p>页面内容</p></AppShell></MemoryRouter></App></QueryClientProvider>);
}
const sidebar=()=>within(screen.getAllByRole('navigation',{name:'主导航'})[0]);

describe('app shell',()=>{
 it('lists the stages in data-flow order with System collapsed at the end',()=>{
  setup('/');
  const labels=Array.from(document.querySelectorAll('.shell-sidebar .sidebar-group-label')).map(node=>node.textContent);
  expect(labels).toEqual(['①采集','②处理','③分析','④策略','⑤Agent 执行','⑥效果','⚙系统']);
  expect(sidebar().getAllByRole('link')[0].textContent).toBe('全链路总览');
  expect(sidebar().getByRole('link',{name:'全链路总览'}).getAttribute('aria-current')).toBe('page');
  expect(sidebar().getAllByRole('link').length).toBe(27);
  expect(sidebar().getByRole('link',{name:'账号簇 / 竞品'}).getAttribute('href')).toBe('/analysis/clusters');
  expect(sidebar().queryByRole('link',{name:'AI 连接'})).toBeNull();
  const toggle=sidebar().getByRole('button',{name:/系统/});
  expect(toggle.getAttribute('aria-expanded')).toBe('false');
  fireEvent.click(toggle);
  expect(sidebar().getAllByRole('link').length).toBe(32);
  expect(sidebar().getByRole('link',{name:/API 参考/}).getAttribute('target')).toBe('_blank');
 });

 it('shows the health of each stage from the pipeline status next to its group heading',()=>{
  setup('/posts',client=>client.setQueryData(['/pipeline/status',{}],{overall:'bad',stages:[
   {key:'collect',status:'ok',headline:'Latest 1 分钟前更新'},{key:'process',status:'warn',headline:'标注积压 2400'},
   {key:'analyze',status:'ok'},{key:'strategy',status:'bad'},{key:'execute',status:'unknown'},{key:'outcome',status:'bad',headline:'低于所需速度'}
  ]}));
  const heading=(label:string)=>Array.from(document.querySelectorAll('.shell-sidebar .sidebar-group-label')).find(node=>node.textContent===label) as HTMLElement;
  expect(within(heading('①采集')).getByRole('img',{name:'状态：正常'}).getAttribute('title')).toBe('Latest 1 分钟前更新');
  expect(within(heading('②处理')).getByRole('img',{name:'状态：注意'})).toBeTruthy();
  expect(heading('④策略').querySelector('.sidebar-dot')?.getAttribute('data-status')).toBe('bad');
  expect(heading('⑤Agent 执行').querySelector('.sidebar-dot')?.getAttribute('data-status')).toBe('unknown');
  // outcome is the sidebar's results group; the overall status sits on the home link without changing its name.
  expect(heading('⑥效果').querySelector('.sidebar-dot')?.getAttribute('data-status')).toBe('bad');
  expect(heading('⚙系统').querySelector('.sidebar-dot')).toBeNull();
  const home=sidebar().getByRole('link',{name:'全链路总览'});
  expect(home.querySelector('.sidebar-dot')?.getAttribute('data-status')).toBe('bad');
 });

 it('shows no dots while the pipeline status is unknown',()=>{
  setup('/posts');
  expect(document.querySelectorAll('.shell-sidebar .sidebar-dot')).toHaveLength(0);
 });

 it('opens System by itself when the current page is inside it',()=>{
  setup('/ai/settings');
  expect(sidebar().getByRole('button',{name:/系统/}).getAttribute('aria-expanded')).toBe('true');
  expect(sidebar().getByRole('link',{name:'AI 连接'}).getAttribute('aria-current')).toBe('page');
 });

 it('highlights the role, not the team queue, on a role page and names the role from loaded data',()=>{
  setup('/ai/agents/maya?tab=memory',client=>client.setQueryData(['/agents',{}],{agents:[{id:'maya',name:'Maya'}]}));
  expect(sidebar().getByRole('link',{name:'角色'}).getAttribute('aria-current')).toBe('page');
  expect(sidebar().getByRole('link',{name:'待处理'}).getAttribute('aria-current')).toBeNull();
  const crumbs=within(screen.getByRole('navigation',{name:'当前位置'}));
  expect(crumbs.getAllByRole('listitem').map(item=>item.textContent)).toEqual(['Agent 执行','角色','Maya','经历记忆']);
  expect(crumbs.getByRole('link',{name:'Maya'}).getAttribute('href')).toBe('/ai/agents/maya');
 });

 it('renders one page heading from the navigation config',()=>{
  setup('/ai/agents?tab=drafts');
  expect(screen.getAllByRole('heading',{level:1}).map(node=>node.textContent)).toEqual(['待处理']);
  expect(sidebar().getByRole('link',{name:'待处理'}).getAttribute('aria-current')).toBe('page');
  expect(screen.getByText('页面内容')).toBeTruthy();
 });

 it('leaves the heading to pages that still render their own',()=>{
  setup('/score');
  expect(screen.queryAllByRole('heading',{level:1})).toHaveLength(0);
  expect(within(screen.getByRole('navigation',{name:'当前位置'})).getAllByRole('listitem').map(item=>item.textContent)).toEqual(['效果','得分与目标']);
 });

 it('refreshes local data from a labelled button',async()=>{
  setup('/posts');
  const invalidate=vi.spyOn(client,'invalidateQueries');
  fireEvent.click(screen.getByRole('button',{name:/刷新/}));
  await waitFor(()=>expect(invalidate).toHaveBeenCalled());
 });

 it('opens the same navigation in a drawer on small screens and closes it after a choice',async()=>{
  setup('/');
  fireEvent.click(screen.getByRole('button',{name:'打开导航'}));
  const drawerNav=await waitFor(()=>{const navs=screen.getAllByRole('navigation',{name:'主导航'});expect(navs.length).toBe(2);return within(navs[1]);});
  fireEvent.click(drawerNav.getByRole('link',{name:'事实库'}));
  await waitFor(()=>expect(screen.getAllByRole('heading',{level:1}).map(node=>node.textContent)).toEqual(['事实库']));
 });
});
