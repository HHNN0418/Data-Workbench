// @vitest-environment jsdom
import {afterEach, describe, expect, it, vi} from 'vitest';
import {cleanup, render, screen} from '@testing-library/react';
import Chart from './Chart';

const chart=vi.hoisted(()=>({on:vi.fn(),off:vi.fn(),setOption:vi.fn(),resize:vi.fn(),dispose:vi.fn()}));
vi.mock('echarts/core',()=>({use:vi.fn(),init:vi.fn(()=>chart)}));

afterEach(()=>{cleanup();vi.clearAllMocks();vi.unstubAllGlobals();});

describe('interactive chart lifecycle',()=>{
 it('replaces callbacks and removes them when the chart unmounts',()=>{
  const disconnect=vi.fn();
  vi.stubGlobal('ResizeObserver',class {observe(){} disconnect=disconnect;});
  const first=vi.fn(),next=vi.fn();
  const view=render(<Chart option={{}} onEvents={{click:first}} ariaLabel="热门排名轨迹"/>);
  expect(screen.getByRole('img',{name:'热门排名轨迹'})).toBeTruthy();
  expect(chart.on).toHaveBeenCalledWith('click',first);
  view.rerender(<Chart option={{}} onEvents={{click:next}}/>);
  expect(chart.off).toHaveBeenCalledWith('click',first);
  expect(chart.on).toHaveBeenCalledWith('click',next);
  view.unmount();
  expect(chart.off).toHaveBeenCalledWith('click',next);
  expect(chart.dispose).toHaveBeenCalledTimes(1);
  expect(disconnect).toHaveBeenCalledTimes(1);
 });
});
