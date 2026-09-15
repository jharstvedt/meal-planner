/**
 * Bouncing dots loader animation.
 * Uses React Native's built-in Animated API.
 */

import { useEffect, useRef } from 'react';
import { Animated, Platform, StyleSheet, View } from 'react-native';
import { spacing, useTheme } from '@/lib/theme';

const useNativeDriver = Platform.OS !== 'web';

interface BouncingLoaderProps {
  color?: string;
  size?: number;
}

export const BouncingLoader = ({ color, size = 12 }: BouncingLoaderProps) => {
  const { colors } = useTheme();
  const dotColor = color ?? colors.accent;
  const bounce1 = useRef(new Animated.Value(0)).current;
  const bounce2 = useRef(new Animated.Value(0)).current;
  const bounce3 = useRef(new Animated.Value(0)).current;

  useEffect(() => {
    const createBounceAnimation = (
      animatedValue: Animated.Value,
      delay: number,
    ) => {
      return Animated.loop(
        Animated.sequence([
          Animated.delay(delay),
          Animated.timing(animatedValue, {
            toValue: -12,
            duration: 300,
            useNativeDriver,
          }),
          Animated.timing(animatedValue, {
            toValue: 0,
            duration: 300,
            useNativeDriver,
          }),
        ]),
      );
    };

    const anim1 = createBounceAnimation(bounce1, 0);
    const anim2 = createBounceAnimation(bounce2, 150);
    const anim3 = createBounceAnimation(bounce3, 300);

    anim1.start();
    anim2.start();
    anim3.start();

    return () => {
      anim1.stop();
      anim2.stop();
      anim3.stop();
    };
  }, [bounce1, bounce2, bounce3]);

  const dotStyle = {
    width: size,
    height: size,
    borderRadius: size / 2,
    backgroundColor: dotColor,
    marginHorizontal: spacing.xs,
  };

  return (
    <View style={styles.container}>
      <Animated.View
        style={[dotStyle, { transform: [{ translateY: bounce1 }] }]}
      />
      <Animated.View
        style={[dotStyle, { transform: [{ translateY: bounce2 }] }]}
      />
      <Animated.View
        style={[dotStyle, { transform: [{ translateY: bounce3 }] }]}
      />
    </View>
  );
};

const styles = StyleSheet.create({
  container: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'center',
    paddingVertical: spacing.xl,
  },
});
