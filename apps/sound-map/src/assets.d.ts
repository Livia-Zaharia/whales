declare module "*.geojson" {
  const geography: import("./map.ts").Countries;
  export default geography;
}
