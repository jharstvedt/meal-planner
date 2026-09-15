import { describe, expect, it } from 'vitest';
import { shouldStackAlertButtons } from '../ThemedAlert';

describe('shouldStackAlertButtons', () => {
  it('stacks when there are more than two buttons on a narrow screen', () => {
    expect(shouldStackAlertButtons(3, 360)).toBe(true);
  });

  it('stacks when there are more than two buttons on wide screens', () => {
    expect(shouldStackAlertButtons(3, 768)).toBe(true);
    expect(shouldStackAlertButtons(4, 1024)).toBe(true);
  });

  it('keeps row layout for one or two buttons', () => {
    expect(shouldStackAlertButtons(1, 360)).toBe(false);
    expect(shouldStackAlertButtons(2, 360)).toBe(false);
    expect(shouldStackAlertButtons(2, 768)).toBe(false);
  });
});
