// @vitest-environment jsdom
import {afterEach, beforeAll, describe, expect, it, vi} from 'vitest';
import {cleanup, fireEvent, render, screen, waitFor} from '@testing-library/react';
import ConfirmButton from './ConfirmButton';
import {antdTheme, applyCssVariables, colors, cssVariables} from './theme';

beforeAll(()=>{
 Object.defineProperty(globalThis,'ResizeObserver',{writable:true,value:class {observe(){} unobserve(){} disconnect(){}}});
 Object.defineProperty(window,'matchMedia',{writable:true,value:vi.fn(query=>({matches:false,media:query,onchange:null,addListener:vi.fn(),removeListener:vi.fn(),addEventListener:vi.fn(),removeEventListener:vi.fn(),dispatchEvent:vi.fn()}))});
 const style=window.getComputedStyle;vi.spyOn(window,'getComputedStyle').mockImplementation(element=>style(element));
});
afterEach(cleanup);

describe('theme',()=>{
 it('exposes every colour as a CSS variable from one definition',()=>{
  expect(Object.keys(cssVariables).sort()).toEqual(Object.keys(colors).sort());
  const root=document.createElement('div');
  applyCssVariables(root);
  expect(root.style.getPropertyValue('--brand')).toBe(colors.brand);
  expect(root.style.getPropertyValue('--stance-no')).toBe(colors.stanceNo);
  expect(antdTheme.token.colorPrimary).toBe(colors.brand);
 });

 it('does not use red and green for the two stances',()=>{
  expect([colors.stanceYes,colors.stanceNo]).not.toContain(colors.ok);
  expect([colors.stanceYes,colors.stanceNo]).not.toContain(colors.danger);
 });
});

describe('ConfirmButton',()=>{
 it('runs the action only after confirmation',async()=>{
  const action=vi.fn();
  render(<ConfirmButton title="删除连接？" description="会一起删除已保存的密钥" okText="删除" onConfirm={action}>删除</ConfirmButton>);
  fireEvent.click(screen.getByRole('button',{name:/删\s*除/}));
  expect(await screen.findByText('会一起删除已保存的密钥')).toBeTruthy();
  expect(action).not.toHaveBeenCalled();
  const buttons=screen.getAllByRole('button',{name:/删\s*除/});
  fireEvent.click(buttons[buttons.length-1]);
  await waitFor(()=>expect(action).toHaveBeenCalledTimes(1));
 });

 it('cannot be opened while disabled',()=>{
  const action=vi.fn();
  render(<ConfirmButton title="删除？" onConfirm={action} disabled>删除</ConfirmButton>);
  fireEvent.click(screen.getByRole('button',{name:/删\s*除/}));
  expect(screen.queryByText('删除？')).toBeNull();
 });
});
