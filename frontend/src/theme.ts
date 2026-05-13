// OpenG2P brand palette — see /repos/branding/guidelines.txt.
import type { ThemeConfig } from "antd";

export const palette = {
  yellow: "#F5BB1A",
  black: "#061327",
  white: "#FFFFFF",
  orange: "#F07B1A",
  magenta: "#88498F",
  grey: "#C4C4C4",
  lightGrey: "#F6F6F6",
  blue: "#2A477A",
};

// Ant Design theme tokens.
export const antdTheme: ThemeConfig = {
  token: {
    colorPrimary: palette.yellow,
    colorInfo: palette.blue,
    colorWarning: palette.orange,
    colorTextBase: palette.black,
    colorBgLayout: palette.lightGrey,
    fontFamily: '"Roboto", system-ui, sans-serif',
    borderRadius: 4,
  },
  components: {
    Layout: {
      headerBg: palette.black,
      headerColor: palette.white,
      headerHeight: 64,
      bodyBg: palette.lightGrey,
      siderBg: palette.white,
    },
    Menu: {
      itemSelectedBg: palette.yellow,
      itemSelectedColor: palette.black,
    },
    Button: {
      colorPrimary: palette.yellow,
      colorPrimaryHover: palette.orange,
      primaryColor: palette.black,
    },
  },
};
